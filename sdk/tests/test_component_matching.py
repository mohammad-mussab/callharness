"""A TTFB sample must be filed under the component that produced it.

Pipecat hands us only the emitting class's NAME (TTFBMetricsData carries
`processor` and `model`, nothing else), so classification is a string match. The
naive version — `"stt" in name` tested before `"tts"` — mis-files any class whose
name happens to contain another component's tag:

    "ElevenLabsTTSService".lower() == "elevenlab*stts*ervice"   -> contains "stt"

Measured against every STT/TTS/LLM service class in pipecat 1.4.0, that mistake
hits 3 of 93: ElevenLabs, Smallest and Speechmatics TTS. The consequence is not a
missing number but a WRONG one — their TTS timings are added to the STT column
while the TTS column stays empty, so the latency view looks populated and is
quietly lying.

Run:  pytest sdk/tests/test_component_matching.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from callharness_sdk.pipecat import _component_for  # noqa: E402


# The three real pipecat classes the old substring match got wrong. These are the
# regression cases — ElevenLabs is the one this project actually runs.
@pytest.mark.parametrize(
    "processor",
    [
        "ElevenLabsTTSService",
        "ElevenLabsTTSService#0",
        "SmallestTTSService",
        "SpeechmaticsTTSService",
    ],
)
def test_tts_classes_containing_stt_are_still_tts(processor):
    assert _component_for(processor) == "tts"


@pytest.mark.parametrize(
    "processor,expected",
    [
        # STT
        ("DeepgramSTTService", "stt"),
        ("SpeechmaticsSTTService", "stt"),
        ("SonioxSTTService", "stt"),
        ("AzureSTTService", "stt"),
        ("AssemblyAISTTService", "stt"),
        # LLM
        ("OpenAILLMService", "llm"),
        ("AnthropicLLMService", "llm"),
        ("BaseOpenAILLMService", "llm"),
        # TTS
        ("CartesiaTTSService", "tts"),
        ("AzureHttpTTSService", "tts"),
        ("ElevenLabsHttpTTSService", "tts"),
        ("PlayHTTTSService", "tts"),
    ],
)
def test_representative_service_names(processor, expected):
    assert _component_for(processor) == expected


def test_subclass_used_by_the_agents():
    """The Trentino/Piemonte agents subclass ElevenLabs TTS to emit a TTFB pipecat
    never emits. Underscores were a workaround for the old matcher; with the
    matcher fixed, both that name and the plain one classify correctly."""
    assert _component_for("ElevenLabs_TTS_WithTTFB") == "tts"
    assert _component_for("ElevenLabsTTSServiceWithTTFB") == "tts"


def test_unknown_and_empty():
    assert _component_for("SomeRandomProcessor") is None
    assert _component_for("") is None
    assert _component_for(None) is None


def test_falls_back_to_loose_match_for_unconventional_names():
    """A custom class that does not end in ...Service still gets classified rather
    than dropped — a guess is better than losing the sample."""
    assert _component_for("MyCustomTtsThing") == "tts"
    assert _component_for("whisper_stt_wrapper") == "stt"


def test_every_real_pipecat_service_name_is_correct():
    """The whole surface, not a hand-picked sample. Each name declares its own
    truth via the tag before "Service", so any future name that breaks the
    matcher fails here.
    """
    import re

    names = [
        "AWSBedrockLLMService", "AWSPollyTTSService", "AWSTranscribeSTTService",
        "AnthropicLLMService", "AssemblyAISTTService", "AsyncAIHttpTTSService",
        "AsyncAITTSService", "AzureHttpTTSService", "AzureLLMService",
        "AzureSTTService", "AzureTTSService", "BaseOpenAILLMService",
        "BaseWhisperSTTService", "CambTTSService", "CartesiaHttpTTSService",
        "CartesiaSTTService", "CartesiaTTSService", "CerebrasLLMService",
        "DeepSeekLLMService", "DeepgramHttpTTSService", "DeepgramSTTService",
        "DeepgramTTSService", "ElevenLabsHttpTTSService", "ElevenLabsTTSService",
        "FireworksLLMService", "FishAudioTTSService", "GladiaSTTService",
        "GoogleLLMService", "GoogleSTTService", "GoogleTTSService",
        "GrokLLMService", "GroqLLMService", "GroqSTTService", "GroqTTSService",
        "InworldTTSService", "LmntTTSService", "MiniMaxTTSService",
        "MistralLLMService", "NeuphonicTTSService", "NimLLMService",
        "OLLamaLLMService", "OpenAILLMService", "OpenAISTTService",
        "OpenAITTSService", "OpenPipeLLMService", "OpenRouterLLMService",
        "PerplexityLLMService", "PiperTTSService", "PlayHTHttpTTSService",
        "PlayHTTTSService", "QwenLLMService", "RimeHttpTTSService",
        "RimeTTSService", "SambaNovaLLMService", "SarvamSTTService",
        "SarvamTTSService", "SmallestTTSService", "SonioxSTTService",
        "SpeechmaticsSTTService", "SpeechmaticsTTSService", "TogetherLLMService",
        "UltravoxSTTService", "WhisperSTTService", "XTTSService",
    ]
    wrong = []
    for name in names:
        match = re.search(r"(STT|TTS|LLM)Service$", name)
        if not match:
            continue
        expected = match.group(1).lower()
        got = _component_for(name)
        if got != expected:
            wrong.append(f"{name}: got {got}, expected {expected}")
    assert not wrong, "mis-classified: " + "; ".join(wrong)
