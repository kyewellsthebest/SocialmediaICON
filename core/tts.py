"""AI narration via OpenAI's gpt-4o-mini-tts.

Two things make this the right model rather than the cheapest one. It bills per
minute of audio (about $0.015), so a few hundred videos a month is a couple of
pounds. And it takes an `instructions` field in plain English - which is the
whole difference between a voice reading a script and a narrator, and costs
about two thousandths of a penny per request.

The instruction string lives in settings so it can be tuned without a deploy;
the default is in core/config.py.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from core.config import settings

log = logging.getLogger(__name__)

API = "https://api.openai.com/v1/audio/speech"
TIMEOUT_S = 120.0
#: The model's own limit on a single request.
MAX_CHARS = 2000


class TTSError(RuntimeError):
    pass


def available() -> bool:
    return bool(settings.openai_api_key)


def cache_dir() -> Path:
    path = Path(settings.work_dir) / "narration"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key(text: str, voice: str, instructions: str, model: str) -> str:
    digest = hashlib.sha256(
        "\x1f".join((model, voice, instructions, text)).encode("utf-8")
    ).hexdigest()
    return digest[:20]


def speak(
    text: str,
    *,
    voice: str | None = None,
    instructions: str | None = None,
    model: str | None = None,
    speed: float | None = None,
) -> Path:
    """Render `text` to an mp3 and return its path.

    Cached on a hash of everything that affects the output, so re-rendering the
    same video does not pay for the same narration twice - and so tweaking one
    line only regenerates that line.
    """
    if not settings.openai_api_key:
        raise TTSError("OPENAI_API_KEY is not set")

    text = " ".join(text.split())
    if not text:
        raise TTSError("nothing to say")
    if len(text) > MAX_CHARS:
        raise TTSError(f"narration line is {len(text)} characters; the model takes {MAX_CHARS}")

    voice = voice or settings.tts_voice
    instructions = instructions if instructions is not None else settings.tts_instructions
    model = model or settings.tts_model

    dest = cache_dir() / f"{_key(text, voice, instructions, model)}.mp3"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    import httpx

    payload: dict[str, object] = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "mp3",
    }
    if instructions:
        payload["instructions"] = instructions
    if speed is not None:
        payload["speed"] = speed

    response = httpx.post(
        API,
        json=payload,
        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        timeout=TIMEOUT_S,
    )
    if response.status_code == 401:
        raise TTSError("OpenAI rejected the key - check OPENAI_API_KEY")
    if response.status_code == 429:
        raise TTSError("OpenAI rate limit or quota reached")
    if response.status_code >= 400:
        raise TTSError(f"OpenAI TTS failed ({response.status_code}): {response.text[:300]}")

    tmp = dest.with_suffix(".part")
    tmp.write_bytes(response.content)
    tmp.replace(dest)
    log.info("narration: %s (%d chars, %d bytes)", dest.name, len(text), len(response.content))
    return dest


#: Roughly what a minute of narration costs, for the dashboard's estimate.
#: gpt-4o-mini-tts is $12 per million audio-output tokens, which works out at
#: about $0.015 a minute. Text input is a rounding error next to it.
COST_PER_MINUTE = 0.015


def estimate_cost(seconds: float) -> float:
    return round(seconds / 60.0 * COST_PER_MINUTE, 4)
