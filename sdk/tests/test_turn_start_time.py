"""`start_time` must be the turn's ONSET, not the moment it finished.

The bug: turns.py derives start_time from the transcript entry's own timestamp,
but an entry only exists once the utterance is COMPLETE (it is skipped while
`text` is empty). So the field named start_time held the turn's END. On a ~8s
greeting that reads as "first bot turn at 10s" when production logs show first
audio at 1.81s — every per-turn timeline was shifted by the length of the turn.

The fix captures BotStartedSpeakingFrame, which the observer already receives,
and applies it to the assistant turn when its text lands.

Run:  pytest sdk/tests/test_turn_start_time.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from callharness_sdk.recorder import CallRecorder  # noqa: E402


def _rec() -> CallRecorder:
    """A recorder with a stub client — these tests never flush."""
    return CallRecorder(client=None, agent_id="test")


def test_recorder_uses_supplied_onset_not_wall_clock():
    """An explicit start_time wins over 'now', so the observer can pass the real
    onset instead of the moment the text happened to arrive."""
    rec = _rec()
    rec.started_at = datetime.now(timezone.utc) - timedelta(seconds=10)

    rec.add_turn(role="assistant", text="Buongiorno, sono l'assistente.", start_time=1.81)

    assert rec.turns[0]["start_time"] == 1.81


def test_recorder_falls_back_to_wall_clock():
    """With no onset supplied the old behaviour stands — a turn recorded ~10s into
    the call is stamped ~10s. This is the path that produced the wrong numbers,
    kept as the fallback for transports that give us nothing better."""
    rec = _rec()
    rec.started_at = datetime.now(timezone.utc) - timedelta(seconds=10)

    rec.add_turn(role="assistant", text="ciao")

    assert rec.turns[0]["start_time"] == pytest.approx(10.0, abs=0.5)


def test_onset_precedes_completion_for_a_long_turn():
    """The actual regression, stated as a property: for an utterance that takes
    time to speak, onset must be strictly less than the completion stamp. If a
    future change reverts to stamping completion, this fails."""
    rec = _rec()
    call_start = datetime.now(timezone.utc) - timedelta(seconds=10)
    rec.started_at = call_start

    onset = 1.81           # bot began speaking here
    completion = 9.9       # ...and finished here (an ~8s greeting)

    rec.add_turn(role="assistant", text="a long greeting", start_time=onset)

    assert rec.turns[0]["start_time"] < completion
    assert rec.turns[0]["start_time"] == onset
