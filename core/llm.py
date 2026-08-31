"""Claude client and the prompts that do the actual judging (spec section 6).

Three calls live here:
  detect_moments  — transcript window  -> candidate clip windows
  rank_candidates — candidate windows  -> predicted_score + rationale
  write_metadata  — one clip's text    -> title, caption, hashtags
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import anthropic

from core.config import settings
from core.selection import Candidate, Window, format_words

log = logging.getLogger(__name__)

# --- prompts --------------------------------------------------------------

DETECT_SYSTEM = (
    "You are a short-form video editor. You receive a transcript with word-level "
    "timestamps. Identify self-contained moments that would make strong vertical "
    "short clips (15-60s). Judge each on: hook (does something grab attention in "
    "the first 3s), emotional intensity (surprise/humour/controversy/curiosity), "
    "payoff (does it land somewhere), context (understandable without the full "
    "video), novelty (not a thing people have seen 500 times). Return STRICT JSON "
    "only - no preamble, no markdown. Clamp all timestamps to values that appear "
    "in the transcript."
)

DETECT_USER = """NICHE: {niche}
TRANSCRIPT WINDOW (start={window_start}s):
{words_with_timestamps}

Return JSON:
{{ "candidates": [ {{
  "start_s": number, "end_s": number,
  "hook_score": 0-10, "emotion": "surprise|humour|controversy|curiosity|excitement|none",
  "payoff_score": 0-10, "context_ok": true|false, "novelty": 0-10,
  "one_line_reason": string
}} ] }}
Only return candidates scoring >=6 on hook OR payoff. Max 8 per window."""

RANK_SYSTEM = (
    "You are a short-form video editor scoring candidate clips before they are "
    "cut. Use the same rubric as selection: hook in the first 3s, emotional "
    "intensity, payoff, standalone context, novelty. Be harsh - a clip that is "
    "merely fine scores in the middle, not the top. Return STRICT JSON only, no "
    "preamble, no markdown."
)

RANK_USER = """NICHE: {niche}
You are scoring {count} candidate clips cut from one source video.

{candidate_blocks}

Return JSON:
{{ "rankings": [ {{
  "id": number,
  "predicted_score": 0-100,
  "rationale": {{ "hook": string, "payoff": string, "risk": string }}
}} ] }}
Return exactly one entry per candidate id above."""

METADATA_SYSTEM = (
    "You write titles and captions for vertical short-form video, in the voice of "
    "someone who actually does this hobby and is posting their own footage.\n\n"
    "Write the way a real person in the community writes:\n"
    "- Say what happened, plainly. The clip is the content; the caption is not a pitch.\n"
    "- Use the vocabulary insiders use, correctly. Getting a term wrong marks you as "
    "an outsider faster than anything else.\n"
    "- Understatement beats hype. A genuinely good find needs no exclamation marks.\n\n"
    "Never do these, because they are what makes an account read as automated:\n"
    "- Engagement bait: 'wait for it', 'you won't believe', 'comment below', "
    "'follow for more', 'part 1'.\n"
    "- Emoji strings, ALL CAPS words, or a wall of hashtags.\n"
    "- Promising something the clip does not actually show.\n"
    "- Describing the video to someone who is already watching it.\n\n"
    "Return STRICT JSON only, no preamble, no markdown."
)

METADATA_USER = """NICHE: {niche}
CLIP TRANSCRIPT:
{clip_text}

Return JSON:
{{ "title": string, "caption": string, "hashtags": [string] }}

title: under 70 characters, sentence case, no trailing punctuation.
caption: one or two short sentences in a normal speaking voice. A question is fine
  only when it is one a real person would genuinely ask the community.
hashtags: {hashtag_count} of them, lowercase, no spaces, each starting with '#'.
  Prefer the specific tags this community actually follows over broad ones like
  #viral or #fyp, which signal spam and reach nobody."""

# --- output schemas -------------------------------------------------------

DETECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_s": {"type": "number"},
                    "end_s": {"type": "number"},
                    "hook_score": {"type": "number"},
                    "emotion": {
                        "type": "string",
                        "enum": [
                            "surprise",
                            "humour",
                            "controversy",
                            "curiosity",
                            "excitement",
                            "none",
                        ],
                    },
                    "payoff_score": {"type": "number"},
                    "context_ok": {"type": "boolean"},
                    "novelty": {"type": "number"},
                    "one_line_reason": {"type": "string"},
                },
                "required": [
                    "start_s",
                    "end_s",
                    "hook_score",
                    "emotion",
                    "payoff_score",
                    "context_ok",
                    "novelty",
                    "one_line_reason",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

RANK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rankings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "predicted_score": {"type": "number"},
                    "rationale": {
                        "type": "object",
                        "properties": {
                            "hook": {"type": "string"},
                            "payoff": {"type": "string"},
                            "risk": {"type": "string"},
                        },
                        "required": ["hook", "payoff", "risk"],
                        "additionalProperties": False,
                    },
                },
                "required": ["id", "predicted_score", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rankings"],
    "additionalProperties": False,
}

METADATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "caption", "hashtags"],
    "additionalProperties": False,
}

# --- client ---------------------------------------------------------------

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model response.

    With `output_config.format` the response is already strict JSON; this also
    survives an SDK or model that fell back to prose-wrapped JSON.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in model response: {text[:200]!r}")
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError(f"unbalanced JSON in model response: {text[:200]!r}")


def _response_text(response: Any) -> str:
    return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")


#: Models that take `thinking: {"type": "adaptive"}` and `output_config.effort`.
#:
#: Both arrived with the 4.6 generation. Sending either to a model from before
#: it is not ignored - it is a 400, and the whole call fails.
#:
#: This cost every verdict for days. verdict.look() sent adaptive thinking and
#: an effort level to claude-haiku-4-5, which is a 4.5 model and takes
#: neither, so every request was rejected before it began. Every clip came
#: back UNWATCHED with an API error on it, and because an unwatched clip was
#: also scored zero on the verdict axis, the whole queue then ranked in the
#: teens. One unsupported parameter, two symptoms that looked unrelated.
ADAPTIVE: tuple[str, ...] = (
    "claude-fable-5", "claude-mythos-5",
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-sonnet-4-6",
)


def thinks_adaptively(model: str) -> bool:
    """Whether this model accepts adaptive thinking and an effort level."""
    return any((model or "").startswith(name) for name in ADAPTIVE)


def json_message(
    system: str, user: str, schema: dict[str, Any], max_tokens: int = 16000
) -> dict[str, Any]:
    """One request, one validated JSON object back."""
    client = get_client()
    base: dict[str, Any] = {
        "model": settings.anthropic_model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
    if thinks_adaptively(settings.anthropic_model):
        base["thinking"] = {"type": "adaptive"}
        if settings.anthropic_effort:
            output_config["effort"] = settings.anthropic_effort

    try:
        response = client.messages.create(**base, output_config=output_config)
    except (TypeError, anthropic.BadRequestError) as exc:
        # Older SDK or a model without structured outputs / adaptive thinking:
        # the prompt already demands strict JSON, so fall back to parsing it.
        log.warning("structured output unavailable (%s); falling back to text JSON", exc)
        base.pop("thinking", None)
        response = client.messages.create(**base)

    return extract_json(_response_text(response))


# --- pipeline calls -------------------------------------------------------


def detect_moments(niche: str, window: Window) -> list[Candidate]:
    payload = json_message(
        DETECT_SYSTEM,
        DETECT_USER.format(
            niche=niche,
            window_start=round(window.start_s, 1),
            words_with_timestamps=format_words(window.words),
        ),
        DETECT_SCHEMA,
    )
    candidates: list[Candidate] = []
    for raw in payload.get("candidates", [])[:8]:
        try:
            candidates.append(
                Candidate(
                    start_s=float(raw["start_s"]),
                    end_s=float(raw["end_s"]),
                    hook_score=float(raw.get("hook_score", 0)),
                    emotion=str(raw.get("emotion", "none")),
                    payoff_score=float(raw.get("payoff_score", 0)),
                    context_ok=bool(raw.get("context_ok", True)),
                    novelty=float(raw.get("novelty", 0)),
                    one_line_reason=str(raw.get("one_line_reason", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("skipping malformed candidate %s: %s", raw, exc)
    return candidates


def rank_candidates(niche: str, candidates: list[Candidate], texts: list[str]) -> list[Candidate]:
    """Score candidates in one call and attach predicted_score + rationale.

    `texts[i]` is the transcript of `candidates[i]`. Candidates the model does
    not return a score for keep their provisional detection score.
    """
    if not candidates:
        return []

    blocks = []
    for i, (cand, text) in enumerate(zip(candidates, texts, strict=True)):
        blocks.append(
            f"--- CANDIDATE id={i} ({cand.start_s:.1f}s -> {cand.end_s:.1f}s, "
            f"{cand.duration_s:.0f}s)\n"
            f"detector said: hook={cand.hook_score} payoff={cand.payoff_score} "
            f"novelty={cand.novelty} emotion={cand.emotion} "
            f"context_ok={cand.context_ok} reason={cand.one_line_reason}\n"
            f"transcript: {text}"
        )

    payload = json_message(
        RANK_SYSTEM,
        RANK_USER.format(
            niche=niche, count=len(candidates), candidate_blocks="\n\n".join(blocks)
        ),
        RANK_SCHEMA,
    )

    for row in payload.get("rankings", []):
        try:
            idx = int(row["id"])
            candidates[idx].predicted_score = float(row["predicted_score"])
            rationale = row.get("rationale") or {}
            candidates[idx].rationale = (
                rationale if isinstance(rationale, dict) else {"note": str(rationale)}
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            log.warning("skipping malformed ranking %s: %s", row, exc)
    return candidates


# --- the studio -----------------------------------------------------------

STUDIO_SYSTEM = (
    "You are choosing one stretch of a real archive recording to build a short "
    "vertical video around, and writing the narration that frames it.\n\n"
    "Two rules matter more than anything else.\n"
    "1. You may only choose a stretch that exists in the transcript below. Every "
    "timestamp you return must be one you can see. The audience will hear the "
    "recording, so a stretch you invented is immediately obvious.\n"
    "2. Your narration must never claim the recording says something it does "
    "not. Describe, set up, and land it - do not paraphrase it as though "
    "quoting. If the transcript is unclear or mundane, say so in `why` and pick "
    "the least bad stretch rather than inventing drama that is not there.\n\n"
    "What makes a stretch worth using: something changes, someone reacts, a "
    "decision is made, or a phrase lands. Procedural chatter with no turn in it "
    "is not a moment, however clearly recorded.\n\n"
    "The narration is three short lines, spoken by a low, unhurried documentary "
    "voice:\n"
    "- hook: three seconds, said before anything is heard. Give a reason to stay "
    "without describing what is coming.\n"
    "- context: said over the title, immediately before the recording plays. "
    "This is the one that has to work hardest - the viewer is about to hear "
    "unfamiliar voices and needs to know who is speaking and what is happening, "
    "or the recording is just noise to them.\n"
    "- closer: said after the recording. Land it or leave it open. Never "
    "summarise what was just heard.\n\n"
    "Return STRICT JSON only - no preamble, no markdown."
)

STUDIO_USER = """ARCHIVE: {name}
SOURCE: {source}

TRANSCRIPT (timestamps in seconds, from the start of this scan):
{words_with_timestamps}

Choose one stretch between {min_s:.0f} and {max_s:.0f} seconds long.

Return JSON:
{{ "start_s": number, "end_s": number,
   "why": string,
   "hook": string, "context": string, "closer": string }}

Each narration line is one sentence, at most 18 words, written to be spoken
aloud. No stage directions, no quotation marks around anything the recording
says."""

STUDIO_SCHEMA = {
    "type": "object",
    "properties": {
        "start_s": {"type": "number"},
        "end_s": {"type": "number"},
        "why": {"type": "string"},
        "hook": {"type": "string"},
        "context": {"type": "string"},
        "closer": {"type": "string"},
    },
    "required": ["start_s", "end_s", "hook", "context", "closer"],
    "additionalProperties": False,
}


def find_moment(
    archive_name: str, archive_source: str, words: list[dict[str, Any]]
) -> dict[str, Any]:
    """Pick the strongest stretch of a recording and write narration for it.

    The transcript goes in whole rather than windowed: these are minutes of
    tape, not hours of podcast, and the judgement is comparative - the best
    stretch is only knowable against the rest.
    """
    from core.tape import DEFAULT_MOMENT_S, MAX_MOMENT_S, MIN_MOMENT_S

    lines: list[str] = []
    for index in range(0, len(words), 12):
        chunk = words[index : index + 12]
        stamp = float(chunk[0]["start"])
        lines.append(f"[{stamp:7.1f}] " + " ".join(w["w"] for w in chunk))

    payload = json_message(
        STUDIO_SYSTEM,
        STUDIO_USER.format(
            name=archive_name,
            source=archive_source,
            words_with_timestamps="\n".join(lines),
            min_s=MIN_MOMENT_S,
            max_s=MAX_MOMENT_S,
        ),
        STUDIO_SCHEMA,
    )

    start = float(payload.get("start_s", 0.0))
    end = float(payload.get("end_s", start + DEFAULT_MOMENT_S))
    if end <= start:
        end = start + DEFAULT_MOMENT_S
    return {
        "start_s": start,
        "end_s": end,
        "why": str(payload.get("why", "")),
        "hook": str(payload.get("hook", "")).strip(),
        "context": str(payload.get("context", "")).strip(),
        "closer": str(payload.get("closer", "")).strip(),
    }


def write_metadata(niche: str, clip_text: str) -> dict[str, Any]:
    payload = json_message(
        METADATA_SYSTEM,
        METADATA_USER.format(
            niche=niche, clip_text=clip_text, hashtag_count=settings.hashtag_count
        ),
        METADATA_SCHEMA,
        max_tokens=4000,
    )
    hashtags = [
        tag if str(tag).startswith("#") else f"#{tag}"
        for tag in payload.get("hashtags", [])
        if str(tag).strip()
    ]
    return {
        "title": str(payload.get("title", "")).strip(),
        "caption": str(payload.get("caption", "")).strip(),
        "hashtags": hashtags,
    }
