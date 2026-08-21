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
    "You write titles, captions and hashtags for vertical short-form video. "
    "Titles are punchy and under 80 characters, no clickbait that the clip does "
    "not deliver on, no emoji spam, no quotation marks wrapping the whole title. "
    "Return STRICT JSON only, no preamble, no markdown."
)

METADATA_USER = """NICHE: {niche}
CLIP TRANSCRIPT:
{clip_text}

Return JSON:
{{ "title": string, "caption": string, "hashtags": [string] }}
5-8 hashtags, each starting with '#', lowercase, no spaces."""

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
        "thinking": {"type": "adaptive"},
    }
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
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


def write_metadata(niche: str, clip_text: str) -> dict[str, Any]:
    payload = json_message(
        METADATA_SYSTEM,
        METADATA_USER.format(niche=niche, clip_text=clip_text),
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
