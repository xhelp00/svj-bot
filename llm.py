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
- Je navazující otázka na předchozí konverzaci s botem o SVJ tématu (např. "a co o víkendech?" po dotazu na pravidla hluku)

Bot NEMÁ odpovědět když zpráva:
- Je běžná konverzace mezi sousedy (pozdravy, smalltalk, vtipy)
- Je osobní zpráva nesouvisející s SVJ (sport, počasí, politika)
- Je odpověď na předchozí konverzaci mezi lidmi (ne s botem)
- Je pouhý emoji, smích, souhlas ("ok", "díky", "👍")
- Je nabídka/prodej osobních věcí
- Týká se financí, poplatků, záloh, vyúčtování, dluhů, plateb nebo rozpočtu SVJ
{context}
Zpráva: "{message}"

Odpověz POUZE jedním slovem: ANO nebo NE"""


def should_respond(message: str, history: list[dict] = None) -> bool:
    """
    Classify whether the bot should respond to a group message.

    Uses a cheap, fast LLM call to decide relevance.
    Includes recent conversation history so follow-up questions are recognized.
    Returns True if the bot should respond.
    """
    model = _get_classifier_model()

    # Build context from recent history (only if bot participated)
    context = ""
    if history:
        # Only include history if the bot actually responded recently
        bot_participated = any(e["role"] == "bot" for e in history)
        if bot_participated:
            context_lines = []
            for entry in history[-4:]:  # last 4 entries max to keep it cheap
                label = "Člen SVJ" if entry["role"] == "user" else "Bot"
                context_lines.append(f"  {label}: {entry['text'][:100]}")
            context = (
                "\nPředchozí konverzace:\n"
                + "\n".join(context_lines)
                + "\n"
            )

    try:
        prompt = CLASSIFIER_PROMPT.format(message=message, context=context)
        response = model.generate_content(prompt)
        answer = response.text.strip().upper()

        result = answer.startswith("ANO")
        logger.info(f"Relevance check: '{message[:60]}...' -> {answer} -> respond={result}")
        return result

    except Exception as e:
        logger.error(f"Classifier error: {e}")
        # If classifier fails, don't respond in group to avoid spam
        return False


FACTCHECK_PROMPT = """Jsi kontrolor faktů pro SVJ (Společenství vlastníků jednotek).

Níže jsou zprávy z dnešní skupinové konverzace a dokumenty SVJ (stanovy, domovní řád, pravidla).

Tvůj úkol:
1. Projdi všechny zprávy od členů SVJ
2. Identifikuj zprávy, kde někdo tvrdí něco o pravidlech, postupech nebo fungování SVJ
3. Porovnej tato tvrzení s dokumenty SVJ
4. Pokud najdeš NEPŘESNOST nebo CHYBU, uveď ji

DŮLEŽITÉ:
- Ignoruj běžnou konverzaci, pozdravy, názory a dotazy
- Zaměř se POUZE na faktická tvrzení o pravidlech SVJ, která jsou v rozporu s dokumenty
- Pokud nikdo netvrdil nic špatně, odpověz prázdným řetězcem
- Nebuď přehnaně přísný — drobné nepřesnosti ve formulaci ignoruj
- Hlásit pouze jasné faktické chyby

Formát odpovědi (pokud najdeš chyby):
• [Jméno] tvrdil: "[co řekl]" — Podle dokumentů SVJ: [co je správně]

Pokud žádné chyby nenajdeš, odpověz POUZE: NONE

--- DOKUMENTY SVJ ---
{knowledge_base}

--- DNEŠNÍ ZPRÁVY ---
{messages}
"""


def fact_check_messages(system_prompt: str, messages: list[dict]) -> str | None:
    """
    Fact-check a day's worth of group messages against SVJ documents.

    Args:
        system_prompt: The knowledge base content
        messages: List of {"sender_name": str, "text": str, "time": str}

    Returns:
        String with findings, or None if no issues found
    """
    model = _get_model()

    # Format messages
    msg_lines = []
    for m in messages:
        msg_lines.append(f"[{m['time']}] {m['sender_name']}: {m['text']}")
    messages_text = "\n".join(msg_lines)

    try:
        prompt = FACTCHECK_PROMPT.format(
            knowledge_base=system_prompt,
            messages=messages_text,
        )
        response = model.generate_content(prompt)
        result = response.text.strip()

        if result.upper() == "NONE" or not result:
            logger.info("Fact-check: no issues found")
            return None

        logger.info(f"Fact-check: found issues ({len(result)} chars)")
        return result

    except Exception as e:
        logger.error(f"Fact-check error: {e}")
        return f"Chyba při fact-checku: {e}"


def generate_response(
    system_prompt: str,
    user_message: str,
    sender_name: str = None,
    history: list[dict] = None,
) -> str:
    """
    Generate a response using Gemini.

    Args:
        system_prompt: The full system prompt with knowledge base
        user_message: The user's question
        sender_name: Unused, kept for API compatibility
        history: List of previous messages [{"role": "user"/"bot", "text": str}]

    Returns:
        The model's response text
    """
    model = _get_model()

    # Build conversation context from history
    context_block = ""
    if history:
        context_lines = []
        for entry in history:
            label = "Člen SVJ" if entry["role"] == "user" else "Asistent"
            context_lines.append(f"{label}: {entry['text']}")
        context_block = (
            "\n\n--- Předchozí konverzace (pro kontext) ---\n"
            + "\n".join(context_lines)
            + "\n--- Konec předchozí konverzace ---\n"
        )

    try:
        response = model.generate_content(
            [
                {
                    "role": "user",
                    "parts": [
                        system_prompt
                        + context_block
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
