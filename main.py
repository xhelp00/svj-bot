"""SVJ WhatsApp Bot - Main FastAPI application."""

import os
import logging

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

        # For group messages, check if bot should respond
        if msg.is_group:
            relevant = should_respond(msg.text)
            if not relevant:
                logger.info(
                    f"Skipping group message from {msg.sender_name}: not relevant"
                )
                return MessageResponse(reply=None)

        # Build/get cached knowledge base
        system_prompt = build_knowledge_base(building_name=BUILDING_NAME)

        # Generate response
        reply = generate_response(
            system_prompt, msg.text, sender_name=msg.sender_name
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
