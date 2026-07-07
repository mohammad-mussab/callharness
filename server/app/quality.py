"""Conversation-quality metrics computed from transcript turn timings.

No LLM required — pure functions over turn start/end times, text, and
interruption flags. Degrades gracefully when timings are missing: speaking
duration is estimated from word count (~2.5 words/second) if end_time absent.
"""

from typing import Any

WORDS_PER_SECOND = 2.5
LONG_SILENCE_SECONDS = 3.0


def _speak_seconds(turn: Any) -> float:
    if turn.start_time is not None and turn.end_time is not None:
        return max(0.0, turn.end_time - turn.start_time)
    return len(turn.text.split()) / WORDS_PER_SECOND


def _effective_end(turn: Any) -> float | None:
    if turn.end_time is not None:
        return turn.end_time
    if turn.start_time is not None:
        return turn.start_time + _speak_seconds(turn)
    return None


def compute_quality(turns: list[Any]) -> dict[str, Any] | None:
    """Compute quality metrics from a list of Turn-like objects
    (need .role, .text, .start_time, .end_time, .interrupted)."""
    if not turns:
        return None

    user_seconds = 0.0
    assistant_seconds = 0.0
    assistant_words = 0
    longest_monologue_words = 0
    interruptions = 0

    for turn in turns:
        seconds = _speak_seconds(turn)
        words = len(turn.text.split())
        if turn.role == "assistant":
            assistant_seconds += seconds
            assistant_words += words
            longest_monologue_words = max(longest_monologue_words, words)
        else:
            user_seconds += seconds
        if turn.interrupted:
            interruptions += 1

    # Silence gaps between consecutive turns
    max_silence = 0.0
    total_silence = 0.0
    long_silences = 0
    for prev, nxt in zip(turns, turns[1:]):
        prev_end = _effective_end(prev)
        if prev_end is None or nxt.start_time is None:
            continue
        gap = nxt.start_time - prev_end
        if gap > 0:
            total_silence += gap
            max_silence = max(max_silence, gap)
            if gap >= LONG_SILENCE_SECONDS:
                long_silences += 1

    return {
        "user_talk_seconds": round(user_seconds, 1),
        "assistant_talk_seconds": round(assistant_seconds, 1),
        "talk_ratio": round(assistant_seconds / user_seconds, 2) if user_seconds > 0 else None,
        "assistant_wpm": round(assistant_words / assistant_seconds * 60) if assistant_seconds > 0 else None,
        "longest_monologue_words": longest_monologue_words,
        "interruption_count": interruptions,
        "max_silence_seconds": round(max_silence, 1),
        "total_silence_seconds": round(total_silence, 1),
        "long_silence_count": long_silences,
        "turn_count": len(turns),
    }
