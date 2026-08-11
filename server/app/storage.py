from pathlib import Path

from .config import settings

AUDIO_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
}


def save_recording(call_id: str, filename: str, content: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in AUDIO_TYPES:
        ext = ".wav"
    path = settings.recordings_dir / f"{call_id}{ext}"
    path.write_bytes(content)
    return str(path)


def content_type_for(path: str) -> str:
    return AUDIO_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def delete_recording(path: str) -> bool:
    """Remove a recording file. True if a file was actually deleted.

    Missing is not an error — a file can be gone because retention already ran, or
    because the volume was replaced. Either way the row should stop claiming it has
    audio, so the caller clears recording_path regardless.
    """
    try:
        target = Path(path)
        if target.is_file():
            target.unlink()
            return True
    except OSError:
        pass
    return False
