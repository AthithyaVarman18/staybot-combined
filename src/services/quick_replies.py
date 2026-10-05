"""
Instant replies for small talk, without calling the AI.

"hi", "thanks", "ok 👍" don't need a language model. Answering them in code
is free and takes milliseconds. Anything that could be an answer to a
question the bot just asked ("ok" after "Would you like to visit?") still
goes to the AI, as does anything longer or unrecognised.
"""

import re


GREETINGS = {
    "hi", "hii", "hiii", "hello", "helo", "hey", "heyy", "hai", "hlo",
    "good morning", "good afternoon", "good evening", "gm",
    "vanakkam", "namaste", "namaskaram", "hi there", "hello there",
}

THANKS = {
    "thanks", "thank you", "thankyou", "thx", "ty", "tq", "thank u",
    "thanks a lot", "thank you so much", "many thanks", "nandri", "dhanyavad",
    "ok thanks", "okay thanks", "ok thank you", "okay thank you",
}

ACKNOWLEDGEMENTS = {"ok", "okay", "k", "kk", "okk", "fine", "cool", "great", "nice", "alright", "noted", "sure"}

GOODBYES = {"bye", "bye bye", "goodbye", "see you", "good night", "gn", "talk later", "ttyl"}

MAX_LENGTH = 30


def normalize(text: str) -> str:
    """Lowercase, drop emoji/punctuation and repeated spaces."""

    text = str(text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)   # 👍 🙏 ! . ,
    return re.sub(r"\s+", " ", text).strip()


def last_assistant_message(history: list) -> str:
    return next(
        (
            str(item.get("content") or "")
            for item in reversed(history or [])
            if isinstance(item, dict) and item.get("role") == "assistant"
        ),
        "",
    )


def quick_reply(message: str, history: list = None):
    """
    Return (kind, reply) for small talk, or None to use the AI.
    kind is one of: greeting, thanks, acknowledgement, goodbye.
    """

    raw = str(message or "").strip()

    if not raw or len(raw) > MAX_LENGTH:
        return None

    text = normalize(raw)
    history = history or []
    bot_asked_question = "?" in last_assistant_message(history)

    # Only emoji, e.g. "👍" or "🙏"
    if not text:
        if bot_asked_question:
            return None
        return "acknowledgement", "👍 Let me know whenever you'd like to see more homes or book a visit."

    if text in GREETINGS:
        if history:
            return "greeting", "Hi again! 👋 What can I help you with?"
        return "greeting", (
            "Hi! 👋 I can help you find a home to rent or buy, book a viewing, "
            "or list your property.\n\n"
            "Tell me what you're looking for, e.g. \"3 bedroom in Garner under $1,800\"."
        )

    # A "yes/ok/sure" right after the bot asked something is an answer, not small talk.
    if bot_asked_question:
        return None

    if text in THANKS:
        return "thanks", "You're welcome! 😊 Message me anytime if you'd like to see more homes or book a visit."

    if text in ACKNOWLEDGEMENTS:
        return "acknowledgement", "👍 Let me know whenever you'd like to see more homes or book a visit."

    if text in GOODBYES:
        return "goodbye", "Bye! 👋 Message me anytime you need help with a home."

    return None
