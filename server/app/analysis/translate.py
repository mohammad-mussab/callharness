"""On-demand transcript translation.

Translates a call's turns to a target language in one LLM pass and caches the
result on each turn (turn.translated_text), so you only pay for translation on
calls you actually read.
"""

import logging

from ..models import Call
from .llm import chat_json

logger = logging.getLogger("opencall.translate")

TRANSLATE_SYSTEM_PROMPT = """You are a professional translator for phone call transcripts. \
You are given numbered lines from a call. Translate each line into the target language, \
preserving tone, names, numbers, dates, and phone numbers exactly. Respond with a single \
JSON object mapping each line number (as a string key) to its translation. Respond with \
JSON only."""


async def translate_call(call: Call, target_language: str = "english") -> int:
    """Translate all turns and store results on turn.translated_text.
    Returns the number of turns translated. Requires call.turns loaded."""
    turns = [t for t in call.turns if t.text.strip()]
    if not turns:
        return 0

    numbered = "\n".join(f"{i}: {t.text}" for i, t in enumerate(turns))
    user_prompt = (
        f"Target language: {target_language.capitalize()}\n\nLines:\n{numbered}"
    )
    result = await chat_json(TRANSLATE_SYSTEM_PROMPT, user_prompt)

    translated = 0
    for i, turn in enumerate(turns):
        value = result.get(str(i))
        if isinstance(value, str) and value.strip():
            turn.translated_text = value.strip()
            translated += 1
    logger.info("Translated %d/%d turns of call %s", translated, len(turns), call.id)
    return translated
