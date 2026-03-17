"""Gemini LLM integration."""

import os
import logging

import google.generativeai as genai

from secret_manager import get_secret

logger = logging.getLogger(__name__)

_model = None
_classifier_model = None


def _get_model():
    """Initialize and return the Gemini model for responses."""
    global _model
    if _model is None:
        genai.configure(api_key=get_secret("GEMINI_API_KEY"))
        _model = genai.GenerativeModel(
            model_name="gemini-2.5-flash-lite",
            generation_config=genai.GenerationConfig(
                temperature=0.3,
                max_output_tokens=1024,
            ),
        )
        logger.info("Gemini model initialized")
    return _model


def _get_classifier_model():
    """Initialize and return a lightweight model for relevance classification."""
    global _classifier_model
    if _classifier_model is None:
        genai.configure(api_key=get_secret("GEMINI_API_KEY"))
        _classifier_model = genai.GenerativeModel(
            model_name="gemini-2.5-flash-lite",
            generation_config=genai.GenerationConfig(
                temperature=0.0,
                max_output_tokens=10,
            ),
        )
        logger.info("Classifier model initialized")
    return _classifier_model


CLASSIFIER_PROMPT = """Jsi klasifikátor zpráv pro chatbota SVJ (Společenství vlastníků jednotek).
Rozhodni, zda má bot na tuto zprávu v SKUPINOVÉM chatu odpovědět.

Bot MÁ odpovědět když zpráva:
- Je dotaz na pravidla domu, stanovy, domovní řád
- Je stížnost nebo hlášení problému (hluk, porucha, závada, úklid)
- Ptá se na postup (rekonstrukce, stěhování, parkování, klíče, odpady)
- Ptá se na kontakty (správce, havárie, výbor)
- Přímo oslovuje bota nebo žádá o pomoc/informaci ohledně SVJ
- Je obecný dotaz relevantní pro bydlení v domě

Bot NEMÁ odpovědět když zpráva:
- Je běžná konverzace mezi sousedy (pozdravy, smalltalk, vtipy)
- Je osobní zpráva nesouvisející s SVJ (sport, počasí, politika)
- Je odpověď na předchozí konverzaci mezi lidmi
- Je pouhý emoji, smích, souhlas ("ok", "díky", "👍")
- Je nabídka/prodej osobních věcí

Zpráva: "{message}"

Odpověz POUZE jedním slovem: ANO nebo NE"""


def should_respond(message: str) -> bool:
    """
    Classify whether the bot should respond to a group message.

    Uses a cheap, fast LLM call to decide relevance.
    Returns True if the bot should respond.
    """
    model = _get_classifier_model()

    try:
        prompt = CLASSIFIER_PROMPT.format(message=message)
        response = model.generate_content(prompt)
        answer = response.text.strip().upper()

        result = answer.startswith("ANO")
        logger.info(f"Relevance check: '{message[:60]}...' -> {answer} -> respond={result}")
        return result

    except Exception as e:
        logger.error(f"Classifier error: {e}")
        # If classifier fails, don't respond in group to avoid spam
        return False


def generate_response(system_prompt: str, user_message: str, sender_name: str = None) -> str:
    """
    Generate a response using Gemini.

    Args:
        system_prompt: The full system prompt with knowledge base
        user_message: The user's question
        sender_name: Unused, kept for API compatibility

    Returns:
        The model's response text
    """
    model = _get_model()

    try:
        response = model.generate_content(
            [
                {
                    "role": "user",
                    "parts": [
                        system_prompt
                        + "\n\n---\n\nDotaz od člena SVJ:\n"
                        + user_message
                    ],
                },
            ]
        )

        reply = response.text.strip()

        # WhatsApp has a 4096 char limit per message
        if len(reply) > 4000:
            reply = reply[:3950] + "\n\n... (zpráva zkrácena, zeptejte se konkrétněji)"

        logger.info(f"Generated response: {len(reply)} chars")
        return reply

    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return (
            "Omlouvám se, došlo k chybě při zpracování vašeho dotazu. "
            "Zkuste to prosím znovu, nebo kontaktujte výbor SVJ."
        )
