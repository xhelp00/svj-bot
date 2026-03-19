"""SVJ WhatsApp Bot - Main FastAPI application."""

import os
import time
import logging
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from pydantic import BaseModel
from dotenv import load_dotenv

from knowledge_base import build_knowledge_base, invalidate_cache
from llm import generate_response, should_respond

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

# --- Conversation history (last 5 messages per chat) ---
MAX_HISTORY = 5
# Key: chat_id (group) or sender (DM) → deque of {"role": "user"/"bot", "text": str}
_conversation_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))

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
        if msg.sender == ADMIN_PHONE and msg.text.strip().lower() == "!reload":
            invalidate_cache()
            system_prompt = build_knowledge_base(building_name=BUILDING_NAME)
            return MessageResponse(
                reply=f"Znalostní báze aktualizována. Načteno {len(system_prompt)} znaků."
            )

        # Rate limiting for DMs only
        if not msg.is_group and _is_rate_limited(msg.sender):
            logger.warning(f"Rate limited: {msg.sender_name} ({msg.sender})")
            return MessageResponse(
                reply="Překročili jste limit zpráv (10 za hodinu). Zkuste to prosím později."
            )

        # For group messages, check if bot should respond
        if msg.is_group:
            relevant = should_respond(msg.text)
            if not relevant:
                logger.info(
                    f"Skipping group message from {msg.sender_name}: not relevant"
                )
                # Still store the message in history for context
                history_key = _get_history_key(msg)
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
