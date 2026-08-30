"""Watch the candidate before cutting it, the way a person would.

Everything upstream of this is arithmetic. Arithmetic can tell that the room
laughed, that the picture surged, that chat sped up - it cannot tell whether
what happened is worth showing anyone. A man laughing at his own joke about
nothing and a man falling off a chair produce the same envelope. The only way
to know which one it was is to look.

So a candidate that survives the cheap signals gets watched: frames sampled
across the moment, the transcript of what was actually said, and the evidence
that nominated it, all handed to a model with the one question that matters -
is anything happening here, and would a stranger watch it.

This is the expensive step and it is placed where expensive steps belong: at
the end, on a handful of candidates a day, after the free signals have thrown
away everything else. The frames are the whole point - a transcript alone
cannot see a reaction and a loudness curve cannot see a face.

It is also the last line before posting. When nothing is being reviewed by a
person any more, this is the only thing standing between a bad clip and an
audience, so a candidate it cannot watch is a candidate that does not go out.
"""

from __future__ import annotations

import base64
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import settings
from core.ffmpeg_ops import require_binaries

log = logging.getLogger(__name__)

#: How many frames to look at. A moment is tens of seconds, so a dozen is one
#: every three or four seconds - enough to see a thing start, happen and land.
#: More is not obviously better: the model is being asked what happened, not to
#: count frames, and each one is real money.
FRAMES = 12
#: Frame width handed to the model. Large enough to read a face and a caption,
#: small enough that a dozen of them is a few thousand tokens.
FRAME_W = 512


class VerdictError(RuntimeError):
    pass


@dataclass
class Verdict:
    """What a look at the actual video says about it."""

    #: Whether the model got to see it at all. False means unknown, not bad -
    #: and unknown is treated as bad wherever posting is automatic.
    watched: bool = False
    worth_it: bool = False
    #: 0..1. How sure it is that a stranger would watch this to the end.
    confidence: float = 0.0
    #: One sentence describing what is actually happening on screen.
    happening: str = ""
    #: What kind of moment - funny, shocking, skilful, an argument, nothing.
    kind: str = ""
    #: Why it is or is not worth clipping, in the model's words.
    why: str = ""
    #: A tighter cut, if the moment turned out to sit inside the window.
    best_start_s: float | None = None
    best_end_s: float | None = None
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "watched": self.watched,
            "worth_it": self.worth_it,
            "confidence": round(self.confidence, 3),
            "happening": self.happening,
            "kind": self.kind,
            "why": self.why,
            "best_start_s": self.best_start_s,
            "best_end_s": self.best_end_s,
            "problems": self.problems,
        }


def sample_frames(
    clip: Path | str, *, count: int = FRAMES, width: int = FRAME_W
) -> list[tuple[float, bytes]]:
    """(timestamp, JPEG) evenly across the clip.

    One ffmpeg pass with an fps filter rather than `count` seeks: seeking into
    a freshly written mp4 `count` times costs `count` decodes of the same
    header, and the timestamps come out uneven anyway.
    """
    require_binaries()
    duration = _duration(clip)
    if duration <= 0:
        raise VerdictError(f"{Path(str(clip)).name} has no duration to sample")

    fps = max(count / duration, 0.05)
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(clip),
            "-vf", f"fps={fps:.4f},scale={width}:-2",
            "-q:v", "6", "-f", "image2pipe", "-vcodec", "mjpeg", "-",
        ],
        capture_output=True,
    )
    if not proc.stdout:
        raise VerdictError(
            f"no frames from {Path(str(clip)).name}: "
            f"{proc.stderr.decode('utf-8', 'replace')[-300:]}"
        )

    frames = _split_jpegs(proc.stdout)[:count]
    if not frames:
        raise VerdictError(f"no complete frames came out of {Path(str(clip)).name}")
    step = duration / max(len(frames), 1)
    return [(round(i * step, 2), data) for i, data in enumerate(frames)]


def _split_jpegs(raw: bytes) -> list[bytes]:
    """Cut a concatenated MJPEG stream back into pictures.

    image2pipe writes them end to end with no length prefix, so the frame
    boundary is the JPEG start marker itself.
    """
    starts = []
    at = raw.find(b"\xff\xd8\xff")
    while at != -1:
        starts.append(at)
        at = raw.find(b"\xff\xd8\xff", at + 3)
    return [raw[a:b] for a, b in zip(starts, starts[1:] + [len(raw)], strict=True)]


def _duration(clip: Path | str) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(clip)],
        capture_output=True,
    )
    try:
        return float(proc.stdout.decode().strip())
    except (ValueError, AttributeError):
        return 0.0


SYSTEM = """You judge whether a moment from a live stream is worth cutting into a
short vertical clip for social media. You are the last check before it is
posted, and nobody looks at it after you.

You are given frames in order across the moment, what was said, and the machine
evidence that nominated it - laughter heard in the audio, a raised voice, a
surge of motion, what chat was doing.

Judge what is actually on screen. The evidence explains why the moment was
brought to you; it is not proof that anything happened. Software hears a laugh
and sees motion; it cannot tell a man laughing at nothing from a man falling
off a chair, and that difference is the entire job.

A clip is worth posting when a stranger with no context would watch it to the
end. That usually means something visibly or audibly happens: a reaction, a
surprise, a physical event, a joke that lands, an argument, a genuinely
impressive play. It is not worth posting when it is someone talking steadily,
a menu or a game lobby, an unchanging screen, a lull, dead air, or a reaction
to something that happened off camera and cannot be seen or heard here.

Be hard to please. Refusing a mediocre moment costs one clip. Posting one
costs the account's credibility. When you are unsure, say so with a low
confidence rather than a confident guess.

If the good part is only a slice of what you were given, say where it starts
and ends in seconds from the beginning of the clip."""


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["happening", "kind", "worth_it", "confidence", "why"],
    "properties": {
        "happening": {
            "type": "string",
            "description": "One sentence: what is actually going on in these frames.",
        },
        "kind": {
            "type": "string",
            "enum": [
                "funny", "shocking", "impressive", "argument", "emotional",
                "awkward", "nothing",
            ],
        },
        "worth_it": {
            "type": "boolean",
            "description": "Would a stranger with no context watch this to the end?",
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "why": {"type": "string", "description": "Why, in one or two sentences."},
        "best_start_s": {
            "type": ["number", "null"],
            "description": "Seconds from the start of the clip where the good part begins.",
        },
        "best_end_s": {"type": ["number", "null"]},
    },
}


def _image_block(data: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def look(
    clip: Path | str,
    *,
    evidence: dict[str, Any] | None = None,
    transcript: str = "",
    quotes: list[str] | None = None,
    count: int = FRAMES,
) -> Verdict:
    """Watch a candidate and say whether it is worth posting.

    Never raises. Anything that stops it looking - no key, no frames, a bad
    response - comes back as `watched=False` with the reason, because the
    caller's decision about an unwatchable clip is a policy question and not
    this function's to make.
    """
    import anthropic

    from core import llm

    problems: list[str] = []
    try:
        frames = sample_frames(clip, count=count)
    except Exception as exc:  # noqa: BLE001 - a blind verdict is a verdict
        return Verdict(problems=[f"could not sample frames: {exc}"])

    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            f"A {_duration(clip):.0f} second moment from a live stream, "
            f"as {len(frames)} frames in order.\n\n"
            f"What the machine heard and saw:\n{_describe(evidence)}\n\n"
            f"What was said:\n{transcript.strip() or '(no transcript available)'}\n\n"
            f"What chat was saying:\n{_describe_quotes(quotes)}"
        ),
    }]
    for at_s, data in frames:
        content.append({"type": "text", "text": f"t = {at_s:.1f}s"})
        content.append(_image_block(data))
    content.append({
        "type": "text",
        "text": "Is anything actually happening here, and is it worth posting?",
    })

    try:
        client = llm.get_client()
        response = client.messages.create(
            model=settings.verdict_model,
            max_tokens=2000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            thinking={"type": "adaptive"},
            output_config={
                "format": {"type": "json_schema", "schema": SCHEMA},
                "effort": settings.verdict_effort,
            },
        )
        payload = llm.extract_json(
            "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        )
    except anthropic.APIStatusError as exc:
        return Verdict(problems=[f"the model refused or errored: {exc}"])
    except Exception as exc:  # noqa: BLE001 - never let a verdict stop the watcher
        return Verdict(problems=[f"{type(exc).__name__}: {exc}"])

    return Verdict(
        watched=True,
        worth_it=bool(payload.get("worth_it")),
        confidence=float(payload.get("confidence") or 0.0),
        happening=str(payload.get("happening") or ""),
        kind=str(payload.get("kind") or ""),
        why=str(payload.get("why") or ""),
        best_start_s=_number(payload.get("best_start_s")),
        best_end_s=_number(payload.get("best_end_s")),
        problems=problems,
    )


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _describe(evidence: dict[str, Any] | None) -> str:
    """The sensed evidence in words, because that is what a prompt reads."""
    if not evidence:
        return "(nothing recorded)"
    heard = evidence.get("heard") or {}
    seen = evidence.get("seen") or {}
    lines = []
    if heard.get("laughs"):
        lines.append(f"- laughter heard {len(heard['laughs'])} time(s)")
    if heard.get("shouts"):
        lines.append(f"- a voice raised {len(heard['shouts'])} time(s)")
    if heard.get("drops"):
        lines.append(f"- the room went abruptly quiet {len(heard['drops'])} time(s)")
    if seen.get("surges"):
        lines.append(f"- the picture moved far more than usual {len(seen['surges'])} time(s)")
    if seen.get("cuts"):
        lines.append(f"- {len(seen['cuts'])} hard cut(s) or camera whips")
    if heard.get("speech_share") is not None:
        lines.append(
            f"- the audio is {round(heard['speech_share'] * 100)}% speech-like, "
            f"{round((heard.get('music_share') or 0) * 100)}% music-like"
        )
    return "\n".join(lines) or "(nothing stood out)"


def _describe_quotes(quotes: list[str] | None) -> str:
    if not quotes:
        return "(chat said nothing notable)"
    return "\n".join(f"- {q}" for q in quotes[:8])
