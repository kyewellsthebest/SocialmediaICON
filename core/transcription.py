"""Word-level transcription via a hosted API.

No local Whisper: Railway has no GPU, and both providers here return word
timestamps, which is the only thing the caption renderer actually needs.
Revisit self-hosted faster-whisper only if this becomes a real line item.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

from core.config import settings

log = logging.getLogger(__name__)

ASSEMBLYAI_BASE = "https://api.assemblyai.com/v2"
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

POLL_INTERVAL_S = 3.0
MAX_WAIT_S = 60 * 60


class TranscriptionError(RuntimeError):
    pass


def _require_key() -> str:
    key = settings.transcription_key
    if not key:
        provider = settings.transcribe_provider
        var = "ASSEMBLYAI_API_KEY" if provider == "assemblyai" else "DEEPGRAM_API_KEY"
        raise TranscriptionError(f"{var} is not set (TRANSCRIBE_PROVIDER={provider})")
    return key


def transcribe(audio_path: Path | str, provider: str | None = None) -> dict[str, Any]:
    """Returns {"words": [{"w","start","end"}], "full_text": str, "provider": str}."""
    provider = provider or settings.transcribe_provider
    if provider == "assemblyai":
        return _assemblyai(Path(audio_path))
    if provider == "deepgram":
        return _deepgram(Path(audio_path))
    raise TranscriptionError(f"unknown transcription provider: {provider}")


def _assemblyai(audio_path: Path) -> dict[str, Any]:
    key = _require_key()
    headers = {"authorization": key}

    with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
        with audio_path.open("rb") as fh:
            upload = client.post(f"{ASSEMBLYAI_BASE}/upload", headers=headers, content=fh)
        upload.raise_for_status()
        audio_url = upload.json()["upload_url"]

        created = client.post(
            f"{ASSEMBLYAI_BASE}/transcript",
            headers=headers,
            json={"audio_url": audio_url, "punctuate": True, "format_text": True},
        )
        created.raise_for_status()
        transcript_id = created.json()["id"]
        log.info("assemblyai transcript %s queued", transcript_id)

        deadline = time.monotonic() + MAX_WAIT_S
        while True:
            poll = client.get(f"{ASSEMBLYAI_BASE}/transcript/{transcript_id}", headers=headers)
            poll.raise_for_status()
            payload = poll.json()
            status = payload.get("status")
            if status == "completed":
                break
            if status == "error":
                raise TranscriptionError(f"assemblyai failed: {payload.get('error')}")
            if time.monotonic() > deadline:
                raise TranscriptionError(f"assemblyai timed out after {MAX_WAIT_S}s")
            time.sleep(POLL_INTERVAL_S)

    words = [
        {"w": w["text"], "start": w["start"] / 1000.0, "end": w["end"] / 1000.0}
        for w in payload.get("words") or []
    ]
    return {
        "words": words,
        "full_text": payload.get("text") or " ".join(w["w"] for w in words),
        "provider": "assemblyai",
    }


def _deepgram(audio_path: Path) -> dict[str, Any]:
    key = _require_key()
    params = {"model": "nova-3", "punctuate": "true", "smart_format": "true"}
    headers = {"Authorization": f"Token {key}", "Content-Type": "audio/mp4"}

    with httpx.Client(timeout=httpx.Timeout(1800.0, connect=30.0)) as client:
        with audio_path.open("rb") as fh:
            response = client.post(DEEPGRAM_URL, params=params, headers=headers, content=fh)
    response.raise_for_status()
    payload = response.json()

    try:
        alt = payload["results"]["channels"][0]["alternatives"][0]
    except (KeyError, IndexError) as exc:
        raise TranscriptionError(f"unexpected deepgram response: {payload}") from exc

    words = [
        {
            "w": w.get("punctuated_word") or w["word"],
            "start": float(w["start"]),
            "end": float(w["end"]),
        }
        for w in alt.get("words", [])
    ]
    return {
        "words": words,
        "full_text": alt.get("transcript") or " ".join(w["w"] for w in words),
        "provider": "deepgram",
    }
