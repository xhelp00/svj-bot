"""SVJ WhatsApp Bot - Main FastAPI application."""

import os
import logging

from fastapi import FastAPI, Request, Response, BackgroundTasks
from dotenv import load_dotenv

from knowledge_base import build_knowledge_base, invalidate_cache
from whatsapp import parse_incoming_message, send_message
from llm import generate_response, should_respond
from secret_manager import get_secret

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="SVJ Bot", version="1.1.0")

# Building name for the system prompt
BUILDING_NAME = os.environ.get("BUILDING_NAME", "SVJ")


@app.get("/")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "bot": "SVJ Bot", "version": "1.1.0"}


@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    WhatsApp webhook verification (GET).
    Meta sends a GET request to verify the webhook URL.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    verify_token = get_secret("WHATSAPP_VERIFY_TOKEN")

    if mode == "subscribe" and token == verify_token:
        logger.info("Webhook verified successfully")
        return Response(content=challenge, media_type="text/plain")
    else:
        logger.warning("Webhook verification failed")
        return Response(content="Forbidden", status_code=403)


@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Handle incoming WhatsApp messages (POST).
    Supports both direct messages and group messages.
    """
    body = await request.json()
    messages = parse_incoming_message(body)

    for msg in messages:
        is_group = msg.get("is_group", False)
        sender_name = msg.get("sender_name", msg["from"])
        source = f"group {msg.get('group_id', '?')}" if is_group else "DM"

        logger.info(
            f"Message from {sender_name} ({msg['from']}) via {source}: "
            f"{msg['text'][:100]}"
        )

        # Process in background so WhatsApp doesn't timeout
        background_tasks.add_task(
            process_message,
            sender=msg["from"],
            text=msg["text"],
            is_group=is_group,
            sender_name=sender_name,
        )

    # Always return 200 quickly to acknowledge receipt
    return {"status": "ok"}


async def process_message(
    sender: str,
    text: str,
    is_group: bool = False,
    sender_name: str = None,
):
    """Process a single incoming message and send a reply."""
    try:
        # Admin commands (only for admin number)
        admin_number = os.environ.get("ADMIN_PHONE", "420720994342")
        if sender == admin_number and text.strip().lower() == "!reload":
            invalidate_cache()
            system_prompt = build_knowledge_base(building_name=BUILDING_NAME)
            await send_message(sender, f"Znalostní báze aktualizována. Načteno {len(system_prompt)} znaků.")
            return

        # For group messages, first check if the bot should respond
        if is_group:
            relevant = should_respond(text)
            if not relevant:
                logger.info(
                    f"Skipping group message from {sender_name}: not relevant"
                )
                return

        # Build/get cached knowledge base
        system_prompt = build_knowledge_base(building_name=BUILDING_NAME)

        # Generate response with sender context
        reply = generate_response(system_prompt, text, sender_name=sender_name)

        # Send reply back to sender (in DM) or group
        await send_message(sender, reply)

    except Exception as e:
        logger.error(f"Error processing message from {sender}: {e}")
        await send_message(
            sender,
            "Omlouvám se, došlo k chybě. Zkuste to prosím znovu později.",
        )


@app.post("/reload")
async def reload_knowledge_base():
    """
    Force reload of the knowledge base from Google Drive.
    Call this after updating documents.
    """
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
