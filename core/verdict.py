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
    #: The model that judged this, so a later comparison knows who said it.
    model: str = ""
    #: One sentence describing what is actually happening on screen.
    happening: str = ""
    #: What kind of moment - funny, shocking, skilful, an argument, nothing.
    kind: str = ""
    #: Where this is and what they are doing. A shocked face at a roulette
    #: wheel is a different clip from a shocked face in a supermarket.
    setting: str = ""
    #: What was on the faces, which is the thing arithmetic cannot reach.
    faces: list[dict[str, Any]] = field(default_factory=list)
    #: Why it is or is not worth clipping, in the model's words.
    why: str = ""
    #: A tighter cut, if the moment turned out to sit inside the window.
    best_start_s: float | None = None
    best_end_s: float | None = None
    #: What the model says was actually in the audio, judged independently of
    #: what the ear claimed. Kept beside the ear's own reading on every clip,
    #: because it is the only real data either will ever get: there is no
    #: recording of anybody laughing in this repository to tune against.
    heard: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "watched": self.watched,
            "worth_it": self.worth_it,
            "confidence": round(self.confidence, 3),
            "model": self.model,
            "happening": self.happening,
            "kind": self.kind,
            "setting": self.setting,
            "faces": self.faces,
            "why": self.why,
            "best_start_s": self.best_start_s,
            "best_end_s": self.best_end_s,
            "heard": self.heard,
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

Read the faces. Where a frame is a close crop of somebody's face, that is
because the machine found a face there and could not tell you what was on it.
Say what you see - shocked, laughing, delighted, furious, bored, embarrassed,
about to cry - and say when a face changes from one to another, because a face
changing is usually the moment itself. Say when you cannot tell.

Say what the situation is, too, not only what the reaction is. Somebody at a
slot machine, at a poker table, walking down a street at night, in a club, in
a car, at a desk, in a ring - the setting is why a reaction means what it
means, and a shocked face at a roulette wheel is a different clip from a
shocked face in a supermarket.

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
and ends in seconds from the beginning of the clip.

Finally, and separately from the judgement: say what you can tell was actually
in the audio - laughter, a gasp, a raised voice, a sigh. Judge that from the
frames and the transcript, not from the machine's reading, and disagree with
the machine freely. Its laughter detector has never been checked against a
recording of anybody laughing, and its gasp and sigh detectors have never been
checked against anything at all; you are the first thing that will ever tell
them whether they are right. If you cannot tell from what you were given, say
so rather than repeating what the machine claimed."""


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["happening", "kind", "worth_it", "confidence", "why", "setting"],
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
        # No minimum/maximum here. The structured-output endpoint rejects
        # both on a number outright - "For 'number' type, properties
        # maximum, minimum are not supported" - with a 400, which fails the
        # whole call rather than the one field. The range goes in the
        # description, and the read below clamps it.
        "confidence": {
            "type": "number",
            "description": "How sure you are, from 0.0 to 1.0.",
        },
        "setting": {
            "type": "string",
            "description": "Where this is and what they are doing - gambling at a "
                           "slot machine, walking a street at night, at a desk, in a "
                           "club, in a ring. One short phrase.",
        },
        "faces": {
            "type": "array",
            "description": "What is on the faces you can see, in order.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "expression": {"type": "string"},
                    "at_s": {"type": ["number", "null"]},
                    "changed_from": {"type": "string"},
                },
            },
        },
        "why": {"type": "string", "description": "Why, in one or two sentences."},
        "best_start_s": {
            "type": ["number", "null"],
            "description": "Seconds from the start of the clip where the good part begins.",
        },
        "best_end_s": {"type": ["number", "null"]},
        "heard": {
            "type": "object",
            "additionalProperties": False,
            "description": "What was actually in the audio, judged independently.",
            "properties": {
                "laughter": {"type": ["boolean", "null"]},
                "gasp": {"type": ["boolean", "null"]},
                "raised_voice": {"type": ["boolean", "null"]},
                "sigh": {"type": ["boolean", "null"]},
                "note": {"type": "string"},
            },
        },
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
    about: str = "",
    said: str = "",
    faces_at: list[float] | None = None,
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
            f"Who this is:\n{about.strip() or '(nothing known about this channel)'}\n\n"
            f"What the machine heard and saw:\n{_describe(evidence)}\n\n"
            f"What was said:\n{transcript.strip() or '(no transcript available)'}\n\n"
            f"{_describe_words(said)}"
            f"What chat was saying:\n{_describe_quotes(quotes)}"
        ),
    }]
    for at_s, data in frames:
        content.append({"type": "text", "text": f"t = {at_s:.1f}s"})
        content.append(_image_block(data))

    # Close crops of the faces the machine found, which is where the answer
    # usually is. A face is a small part of a wide frame, so a whole frame
    # spends most of its tokens on the wall behind somebody's head.
    for at_s, data in _face_crops(clip, where=faces_at, count=count // 3):
        content.append({"type": "text", "text": f"a face at t = {at_s:.1f}s"})
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
            # Cached: this prompt is identical on every look and it is a
            # quarter of the input. Cache reads are a tenth of the price, so
            # it pays for itself on the second call of the day and every one
            # after it. It has to stay byte-identical to keep doing so - no
            # timestamps, no channel name, nothing per-call. All of that is in
            # the message, which comes after.
            system=[{
                "type": "text",
                "text": SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
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
        # Which model said so, stored on the clip. The model tier is a cost
        # decision that can be revisited, and revisiting it means comparing
        # what two of them said about comparable clips - which is impossible
        # after the fact unless each verdict remembers its own author.
        model=getattr(response, "model", "") or settings.verdict_model,
        worth_it=bool(payload.get("worth_it")),
        # Clamped here rather than in the schema, which cannot express a range.
        confidence=min(1.0, max(0.0, float(payload.get("confidence") or 0.0))),
        happening=str(payload.get("happening") or ""),
        kind=str(payload.get("kind") or ""),
        setting=str(payload.get("setting") or ""),
        faces=payload.get("faces") or [],
        why=str(payload.get("why") or ""),
        best_start_s=_number(payload.get("best_start_s")),
        best_end_s=_number(payload.get("best_end_s")),
        heard=payload.get("heard") or {},
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
    if heard.get("gasps"):
        lines.append(
            f"- what may be a sharp intake of breath, {len(heard['gasps'])} time(s) "
            "(this detector is unproven - say if it is wrong)"
        )
    if heard.get("sighs"):
        lines.append(
            f"- what may be a long breath out, {len(heard['sighs'])} time(s) "
            "(also unproven)"
        )
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


def _describe_words(said: str) -> str:
    """What was being said in the seconds around the moment, if it was heard live."""
    if not said.strip():
        return ""
    return f"What was said right at the moment:\n{said.strip()}\n\n"


def _describe_quotes(quotes: list[str] | None) -> str:
    if not quotes:
        return "(chat said nothing notable)"
    return "\n".join(f"- {q}" for q in quotes[:8])


def _face_crops(
    clip: Path | str, *, where: list[float] | None, count: int = 4, width: int = 320
) -> list[tuple[float, bytes]]:
    """Close crops of the faces the machine found, as JPEGs.

    A face is a small part of a wide frame, so a whole frame spends most of its
    tokens on the wall behind somebody's head. These are where the answer
    usually is: what is on the face, and whether it changed.

    Returns nothing rather than raising - a missing crop should cost a little
    accuracy on the expression, not the clip.
    """
    if not where:
        return []
    require_binaries()
    picked = sorted(where)[: max(1, count)]
    out: list[tuple[float, bytes]] = []
    for at_s in picked:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, at_s):.2f}",
             "-i", str(clip), "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-q:v", "5",
             "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True,
        )
        if proc.stdout.startswith(b"\xff\xd8\xff"):
            out.append((at_s, proc.stdout))
    return out
