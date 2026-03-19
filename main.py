"""SVJ WhatsApp Bot - Main FastAPI application."""

import os
import time
import logging
from datetime import datetime, date
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel
from dotenv import load_dotenv

from knowledge_base import build_knowledge_base, invalidate_cache
from llm import generate_response, should_respond, fact_check_messages

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="SVJ Bot", version="2.0.0")

# Building name for the system prompt
BUILDING_NAME = os.environ.get("BUILDING_NAME", "SVJ")
ADMIN_PHONE = os.environ.get("ADMIN_PHONE", "420720994342")

# Whitelist of allowed group JIDs. If empty, bot responds in any group.
# Format: comma-separated list of WhatsApp group JIDs (e.g. "123456789-987654321@g.us")
_allowed_groups_raw = os.environ.get("ALLOWED_GROUP_IDS", "")
ALLOWED_GROUP_IDS: set[str] = {
    g.strip() for g in _allowed_groups_raw.split(",") if g.strip()
}

# --- Conversation history (last 5 messages per chat) ---
MAX_HISTORY = 5
# Key: chat_id (group) or sender (DM) → deque of {"role": "user"/"bot", "text": str}
_conversation_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))

# --- Daily message log for fact-checking ---
# Stores all group messages for the current day: list of {"sender_name": str, "text": str, "time": str}
_daily_messages: list[dict] = []
_daily_messages_date: str = ""  # YYYY-MM-DD of current log

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://whatsapp-bridge:3000")


def _log_daily_message(sender_name: str, text: str):
    """Store a group message in the daily log for fact-checking."""
    global _daily_messages, _daily_messages_date
    today = date.today().isoformat()
    # Reset log on new day
    if _daily_messages_date != today:
        _daily_messages = []
        _daily_messages_date = today
    _daily_messages.append({
        "sender_name": sender_name,
        "text": text,
        "time": datetime.now().strftime("%H:%M"),
    })


async def _send_admin_dm(text: str):
    """Send a direct message to the admin via the WhatsApp bridge."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{BRIDGE_URL}/send",
                json={"to": f"{ADMIN_PHONE}@c.us", "text": text},
                timeout=30,
            )
            logger.info(f"Admin DM sent: {resp.status_code}")
    except Exception as e:
        logger.error(f"Failed to send admin DM: {e}")


# --- Rate limiting (DM only) ---
RATE_LIMIT_MAX = 10  # max messages per window
RATE_LIMIT_WINDOW = 3600  # 1 hour in seconds
# Key: sender → list of timestamps
_rate_timestamps: dict[str, list[float]] = defaultdict(list)


def _is_rate_limited(sender: str) -> bool:
    """Check if a DM sender has exceeded the rate limit."""
    now = time.time()
    # Prune old timestamps
    _rate_timestamps[sender] = [
        t for t in _rate_timestamps[sender] if now - t < RATE_LIMIT_WINDOW
    ]
    if len(_rate_timestamps[sender]) >= RATE_LIMIT_MAX:
        return True
    _rate_timestamps[sender].append(now)
    return False


def _get_history_key(msg) -> str:
    """Return the conversation key: chat_id for groups, sender for DMs."""
    if msg.is_group and msg.chat_id:
        return msg.chat_id
    return msg.sender


# Phrases that indicate the bot doesn't have a useful answer
_UNCERTAIN_PHRASES = [
    "nemohu odpovídat",
    "nemohu odpovědět",
    "nemám k dispozici",
    "nemám informace",
    "nenalezl jsem",
    "nenašel jsem",
    "není v dokumentech",
    "není v mých dokumentech",
    "obraťte se na výbor",
    "kontaktujte výbor",
    "kontaktujte správce",
    "doporučuji kontaktovat",
    "doporučuji obrátit se",
    "nemohu poskytnout",
    "nemám dostatek informací",
    "tuto informaci nemám",
    "bohužel nemám",
    "bohužel nemohu",
    "na dotazy týkající se financí",
]


def _is_uncertain_response(reply: str) -> bool:
    """Check if the bot's reply indicates it doesn't know the answer."""
    reply_lower = reply.lower()
    return any(phrase in reply_lower for phrase in _UNCERTAIN_PHRASES)


class MessageRequest(BaseModel):
    text: str
    sender: str
    sender_name: str = ""
    is_group: bool = False
    chat_id: str = ""


class MessageResponse(BaseModel):
    reply: str | None = None


@app.get("/")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "bot": "SVJ Bot", "version": "2.0.0"}


@app.post("/message", response_model=MessageResponse)
async def handle_message(msg: MessageRequest):
    """
    Handle incoming message from the WhatsApp bridge.
    Returns a reply or null if bot should not respond.
    """
    source = "GROUP" if msg.is_group else "DM"
    logger.info(
        f"Message from {msg.sender_name} ({msg.sender}) via {source}: "
        f"{msg.text[:100]}"
    )

    try:
        # Admin commands
        if msg.sender == ADMIN_PHONE:
            cmd = msg.text.strip().lower()
            if cmd == "!reload":
                invalidate_cache()
                system_prompt = build_knowledge_base(building_name=BUILDING_NAME)
                return MessageResponse(
                    reply=f"Znalostní báze aktualizována. Načteno {len(system_prompt)} znaků."
                )
            if cmd == "!factcheck":
                await _run_factcheck()
                return MessageResponse(
                    reply=f"Fact-check spuštěn pro {len(_daily_messages)} zpráv. Výsledek přijde v DM."
                )

        # Reject messages from groups not in the whitelist (second line of defence;
        # the bridge should have already left such groups via group_join handler)
        if msg.is_group and ALLOWED_GROUP_IDS and msg.chat_id not in ALLOWED_GROUP_IDS:
            logger.warning(
                f"Message from unauthorized group {msg.chat_id}, ignoring"
            )
            return MessageResponse(reply=None)

        # Rate limiting for DMs only
        if not msg.is_group and _is_rate_limited(msg.sender):
            logger.warning(f"Rate limited: {msg.sender_name} ({msg.sender})")
            return MessageResponse(
                reply="Překročili jste limit zpráv (10 za hodinu). Zkuste to prosím později."
            )

        # Log all group messages for daily fact-checking
        if msg.is_group:
            _log_daily_message(msg.sender_name, msg.text)

        # For group messages, check if bot should respond
        if msg.is_group:
            history_key = _get_history_key(msg)
            history = list(_conversation_history[history_key])
            relevant = should_respond(msg.text, history=history)
            if not relevant:
                logger.info(
                    f"Skipping group message from {msg.sender_name}: not relevant"
                )
                # Still store the message in history for context
                _conversation_history[history_key].append(
                    {"role": "user", "text": msg.text}
                )
                return MessageResponse(reply=None)

        # Build/get cached knowledge base
        system_prompt = build_knowledge_base(building_name=BUILDING_NAME)

        # Get conversation history
        history_key = _get_history_key(msg)
        history = list(_conversation_history[history_key])

        # Generate response with context
        reply = generate_response(
            system_prompt, msg.text, sender_name=msg.sender_name, history=history
        )

        # In groups, suppress "I don't know" responses — only reply when adding value
        if msg.is_group and reply and _is_uncertain_response(reply):
            logger.info(
                f"Suppressing uncertain response in group for: {msg.text[:60]}"
            )
            _conversation_history[history_key].append(
                {"role": "user", "text": msg.text}
            )
            return MessageResponse(reply=None)

        # Store both user message and bot reply in history
        _conversation_history[history_key].append(
            {"role": "user", "text": msg.text}
        )
        if reply:
            _conversation_history[history_key].append(
                {"role": "bot", "text": reply}
            )

        return MessageResponse(reply=reply)

    except Exception as e:
        logger.error(f"Error processing message from {msg.sender}: {e}")
        return MessageResponse(
            reply="Omlouvám se, došlo k chybě. Zkuste to prosím znovu později."
        )


async def _run_factcheck():
    """Run fact-check on today's group messages and DM results to admin."""
    if not _daily_messages:
        await _send_admin_dm("📋 Fact-check: Dnes nebyly žádné zprávy ve skupině.")
        return

    system_prompt = build_knowledge_base(building_name=BUILDING_NAME)
    result = fact_check_messages(system_prompt, _daily_messages)

    if result:
        await _send_admin_dm(f"📋 Denní fact-check ({_daily_messages_date}):\n\n{result}")
    else:
        await _send_admin_dm(
            f"📋 Fact-check ({_daily_messages_date}): "
            f"Zkontrolováno {len(_daily_messages)} zpráv, žádné nepřesnosti nenalezeny. ✅"
        )


@app.post("/factcheck")
async def trigger_factcheck():
    """Trigger daily fact-check (called by cron or manually)."""
    await _run_factcheck()
    return {"status": "done", "messages_checked": len(_daily_messages)}


@app.post("/reload")
async def reload_knowledge_base():
    """Force reload of the knowledge base from Google Drive."""
    invalidate_cache()
    kb = build_knowledge_base(building_name=BUILDING_NAME)
    return {
        "status": "reloaded",
        "knowledge_base_size": len(kb),
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
