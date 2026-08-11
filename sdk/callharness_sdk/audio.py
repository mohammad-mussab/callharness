"""Collect call audio and wrap it as a playable WAV.

`CallRecorder` uses this internally, so integrations built on it need nothing here.
It is exposed separately for agents that already have their own call pipeline and
upload through the REST client directly — the same reason `LatencyCollector` exists.

    from callharness_sdk import AudioCollector
    from callharness_sdk.pipecat import attach_audio

    audio = AudioCollector()
    attach_audio(audio, audio_buffer)      # any object with .add_audio() works
    ...
    client.upload_recording_bytes(call_id, audio.wav())

Audio is held in memory rather than written to a file: a voice-agent container is
usually ephemeral and often read-only, and a call's audio is a few megabytes at most.
"""

import io
import wave

__all__ = ["AudioCollector"]

# AudioBufferProcessor emits signed 16-bit little-endian PCM.
_SAMPLE_WIDTH_BYTES = 2


class AudioCollector:
    """Accumulates raw PCM chunks and renders them as a WAV."""

    def __init__(self, sample_rate: int = 16000, num_channels: int = 1) -> None:
        self._pcm = bytearray()
        self.sample_rate = sample_rate
        self.num_channels = num_channels

    def add_audio(self, pcm: bytes, sample_rate: int = 0, num_channels: int = 0) -> None:
        """Append a chunk.

        Safe to call repeatedly: Pipecat fires once at ``stop_recording()`` when
        ``buffer_size=0``, but continuously during the call when it is set, so chunks
        are appended rather than replaced. The stream's real rate and channel count
        arrive with the data, so they override the constructor defaults.
        """
        if not pcm:
            return
        self._pcm.extend(pcm)
        if sample_rate:
            self.sample_rate = sample_rate
        if num_channels:
            self.num_channels = num_channels

    @property
    def has_audio(self) -> bool:
        return len(self._pcm) > 0

    @property
    def duration_seconds(self) -> float:
        frame_bytes = self.sample_rate * _SAMPLE_WIDTH_BYTES * max(1, self.num_channels)
        return len(self._pcm) / frame_bytes if frame_bytes else 0.0

    def wav(self) -> bytes | None:
        """The accumulated audio as a playable WAV, or None if nothing was captured.

        Uses the stdlib `wave` module — the PCM is already the right shape, so this
        only prepends a header and the SDK stays dependency-free.
        """
        if not self._pcm:
            return None
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(max(1, self.num_channels))
            wav.setsampwidth(_SAMPLE_WIDTH_BYTES)
            wav.setframerate(self.sample_rate)
            wav.writeframes(bytes(self._pcm))
        return buffer.getvalue()

    def clear(self) -> None:
        self._pcm.clear()

    def __len__(self) -> int:
        return len(self._pcm)
