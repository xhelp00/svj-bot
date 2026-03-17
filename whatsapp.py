"""WhatsApp Business API integration."""

import os
import logging

import httpx

from secret_manager import get_secret

logger = logging.getLogger(__name__)

WHATSAPP_API_URL = "https://graph.facebook.com/v21.0"


async def send_message(to: str, text: str):
    """
    Send a text message via WhatsApp Business API.

    Args:
        to: Recipient phone number or group chat ID
        text: Message text to send
    """
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    access_token = get_secret("WHATSAPP_ACCESS_TOKEN")

    url = f"{WHATSAPP_API_URL}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=30)

        if response.status_code == 200:
            logger.info(f"Message sent to {to}")
        else:
            logger.error(
                f"Failed to send message to {to}: "
                f"{response.status_code} - {response.text}"
            )


def parse_incoming_message(body: dict) -> list[dict]:
    """
    Parse incoming WhatsApp webhook payload.
    Handles both direct messages and group messages.

    Returns list of dicts with keys:
        - from: sender phone number
        - text: message text
        - message_id: WhatsApp message ID
        - is_group: whether this is a group message
        - group_id: group chat ID (if group message)
        - sender_name: sender's profile name (if available)
    """
    messages = []

    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # Build a contacts lookup for sender names
                contacts = {}
                for contact in value.get("contacts", []):
                    wa_id = contact.get("wa_id", "")
                    name = contact.get("profile", {}).get("name", "")
                    contacts[wa_id] = name

                for msg in value.get("messages", []):
                    if msg.get("type") != "text":
                        continue

                    sender = msg["from"]
                    is_group = "group_id" in msg.get("context", {}) or msg.get("from") != msg.get("from")

                    # In group chats, there's a "context" with group info
                    # The "from" field is always the sender's number
                    # Group ID comes from the metadata
                    group_id = None
                    if "context" in msg:
                        group_id = msg["context"].get("from")

                    # Check if this is a group message based on the metadata
                    metadata = value.get("metadata", {})

                    messages.append(
                        {
                            "from": sender,
                            "text": msg["text"]["body"],
                            "message_id": msg["id"],
                            "is_group": group_id is not None,
                            "group_id": group_id,
                            "sender_name": contacts.get(sender, sender),
                            "reply_to": sender,  # In DM, reply to sender; in group, also reply to sender
                        }
                    )
    except (KeyError, TypeError) as e:
        logger.error(f"Error parsing webhook payload: {e}")

    return messages
