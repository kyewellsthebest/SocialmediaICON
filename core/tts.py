"""AI narration, from OpenAI or ElevenLabs.

OpenAI's gpt-4o-mini-tts is the cheap default: about $0.015 a minute, and it
takes an `instructions` field in plain English, which is most of what stops a
voice sounding like an assistant reading a script.

ElevenLabs is the better voice and costs roughly four pounds a month more at
ten videos a day. It has no instructions field - delivery is a property of the
voice you pick - so switching providers is not only a quality change, it moves
where the direction lives.

Both cache on a hash of everything that affects the output, so re-rendering a
video does not pay for the same narration twice.
"""

from __future__ import annotations

import hashlib
import logging
from functools import lru_cache
from pathlib import Path

from core.config import settings

log = logging.getLogger(__name__)

OPENAI_URL = "https://api.openai.com/v1/audio/speech"
ELEVEN_BASE = "https://api.elevenlabs.io/v1"
TIMEOUT_S = 120.0
#: gpt-4o-mini-tts's own limit on a single request.
MAX_CHARS = 2000


class TTSError(RuntimeError):
    pass


def backend() -> str:
    return settings.tts_backend


def available() -> bool:
    return settings.has_tts


def describe() -> dict[str, object]:
    """What the dashboard shows about the voice, without leaking a key."""
    if backend() == "elevenlabs":
        return {
            "provider": "elevenlabs",
            "voice": settings.elevenlabs_voice,
            "model": settings.elevenlabs_model,
            "var": "ELEVENLABS_API_KEY",
            "set": bool(settings.elevenlabs_api_key),
            "per_minute_usd": ELEVEN_COST_PER_MINUTE,
            "steerable": False,
        }
    return {
        "provider": "openai",
        "voice": settings.tts_voice,
        "model": settings.tts_model,
        "var": "OPENAI_API_KEY",
        "set": bool(settings.openai_api_key),
        "per_minute_usd": OPENAI_COST_PER_MINUTE,
        "steerable": True,
    }


def cache_dir() -> Path:
    path = Path(settings.work_dir) / "narration"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]


# --- ElevenLabs ------------------------------------------------------------


@lru_cache(maxsize=8)
def resolve_voice(name: str) -> str:
    """Turn a voice name into its id.

    Ids are opaque and differ per account, so the setting is a name and this
    looks it up once. A name that matches nothing raises with the list of what
    the account actually has, which is more use than a 404 from a voice id.
    """
    import httpx

    if not settings.elevenlabs_api_key:
        raise TTSError("ELEVENLABS_API_KEY is not set")

    # A raw id was given rather than a name - ElevenLabs ids are 20 characters
    # of base62, so anything that shape is passed straight through.
    if len(name) == 20 and name.isalnum():
        return name

    response = httpx.get(
        f"{ELEVEN_BASE}/voices",
        headers={"xi-api-key": settings.elevenlabs_api_key},
        timeout=TIMEOUT_S,
    )
    if response.status_code == 401:
        raise TTSError("ElevenLabs rejected the key - check ELEVENLABS_API_KEY")
    response.raise_for_status()

    voices = response.json().get("voices", []) or []
    wanted = name.strip().lower()
    for voice in voices:
        if str(voice.get("name", "")).strip().lower() == wanted:
            return str(voice["voice_id"])

    known = ", ".join(sorted(str(v.get("name", "?")) for v in voices)) or "none"
    raise TTSError(f"no ElevenLabs voice named {name!r}. Available: {known}")


def _speak_elevenlabs(text: str, voice: str, model: str) -> bytes:
    import httpx

    voice_id = resolve_voice(voice)
    response = httpx.post(
        f"{ELEVEN_BASE}/text-to-speech/{voice_id}",
        headers={
            "xi-api-key": settings.elevenlabs_api_key or "",
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        json={
            "text": text,
            "model_id": model,
            # Stability high and style low: this is documentary narration under
            # a recording, not a performance. The default settings read as an
            # audiobook actor and pull focus from the tape.
            "voice_settings": {
                "stability": 0.55,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            },
        },
        timeout=TIMEOUT_S,
    )
    if response.status_code == 401:
        raise TTSError("ElevenLabs rejected the key - check ELEVENLABS_API_KEY")
    if response.status_code == 429:
        raise TTSError("ElevenLabs quota or rate limit reached")
    if response.status_code >= 400:
        raise TTSError(f"ElevenLabs failed ({response.status_code}): {response.text[:300]}")
    return response.content


# --- OpenAI ----------------------------------------------------------------


def _speak_openai(
    text: str, voice: str, model: str, instructions: str, speed: float | None
) -> bytes:
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
        OPENAI_URL,
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
    return response.content


# --- the one entry point ---------------------------------------------------


def speak(
    text: str,
    *,
    voice: str | None = None,
    instructions: str | None = None,
    model: str | None = None,
    speed: float | None = None,
) -> Path:
    """Render `text` to an mp3 and return its path."""
    text = " ".join(text.split())
    if not text:
        raise TTSError("nothing to say")
    if len(text) > MAX_CHARS:
        raise TTSError(f"narration line is {len(text)} characters; the limit is {MAX_CHARS}")

    which = backend()
    if which == "elevenlabs":
        voice = voice or settings.elevenlabs_voice
        model = model or settings.elevenlabs_model
        instructions = ""  # ElevenLabs has no equivalent; delivery is the voice
        if not settings.elevenlabs_api_key:
            raise TTSError("ELEVENLABS_API_KEY is not set (TTS_PROVIDER=elevenlabs)")
    else:
        voice = voice or settings.tts_voice
        model = model or settings.tts_model
        instructions = settings.tts_instructions if instructions is None else instructions
        if not settings.openai_api_key:
            raise TTSError("OPENAI_API_KEY is not set")

    dest = cache_dir() / f"{_key(which, model, voice, instructions, text)}.mp3"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    if which == "elevenlabs":
        audio = _speak_elevenlabs(text, voice, model)
    else:
        audio = _speak_openai(text, voice, model, instructions, speed)

    tmp = dest.with_suffix(".part")
    tmp.write_bytes(audio)
    tmp.replace(dest)
    log.info("narration: %s via %s (%d chars, %d bytes)", dest.name, which, len(text), len(audio))
    return dest


#: gpt-4o-mini-tts is $12 per million audio-output tokens, about $0.015/min.
OPENAI_COST_PER_MINUTE = 0.015
#: ElevenLabs bills per character; at ~15 characters a second of speech, their
#: $0.05-per-1k-character tiers land near this per minute of narration.
ELEVEN_COST_PER_MINUTE = 0.045


def cost_per_minute() -> float:
    return ELEVEN_COST_PER_MINUTE if backend() == "elevenlabs" else OPENAI_COST_PER_MINUTE


def estimate_cost(seconds: float) -> float:
    return round(seconds / 60.0 * cost_per_minute(), 4)


#: Kept for callers that predate the provider switch.
COST_PER_MINUTE = OPENAI_COST_PER_MINUTE
