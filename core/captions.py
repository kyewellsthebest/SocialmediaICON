"""Word timestamps -> ASS subtitles with karaoke-style word highlighting.

Layout targets a 1080x1920 frame and keeps every line inside the vertical safe
zone: nothing in the top 15% or the bottom 20%, where the platform UI (profile
row, caption text, buttons) sits.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

Word = dict[str, Any]

PLAY_W = 1080
PLAY_H = 1920
TOP_UNSAFE = 0.15
BOTTOM_UNSAFE = 0.20


@dataclass
class CaptionStyle:
    font: str = "DejaVu Sans"
    font_size: int = 82
    primary_colour: str = "&H00FFFFFF"  # white  (ASS is &HAABBGGRR)
    highlight_colour: str = "&H0000E5FF"  # amber
    outline_colour: str = "&H00000000"
    back_colour: str = "&H80000000"
    bold: int = 1
    outline: int = 6
    shadow: int = 2
    margin_h: int = 90
    # Distance from the bottom of the frame to the bottom of the text. The
    # bottom 20% is platform UI, so start above it.
    margin_v: int = int(PLAY_H * BOTTOM_UNSAFE) + 40
    max_chars: int = 28
    max_words: int = 5
    max_line_s: float = 3.0
    max_gap_s: float = 0.7
    # How long a finished line stays up during a pause before the next line.
    # Without this the caption blinks out between lines.
    line_hold_s: float = 0.4


def format_ts(seconds: float) -> str:
    """ASS timestamp: H:MM:SS.cc"""
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    centis = int(round((secs - int(secs)) * 100))
    secs = int(secs)
    if centis == 100:  # rounding carry
        centis = 0
        secs += 1
        if secs == 60:
            secs = 0
            minutes += 1
    return f"{int(hours)}:{int(minutes):02d}:{secs:02d}.{centis:02d}"


def escape_text(text: str) -> str:
    """ASS treats braces as override blocks and backslashes as escapes."""
    return (
        str(text)
        .replace("\\", "/")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", " ")
        .strip()
    )


def group_words_into_lines(
    words: list[Word], style: CaptionStyle | None = None
) -> list[list[Word]]:
    """Break the word stream into short on-screen lines.

    A line ends on any of: word count, character count, elapsed time, or a
    pause long enough that keeping the words together would read wrong.
    """
    style = style or CaptionStyle()
    lines: list[list[Word]] = []
    current: list[Word] = []

    for word in words:
        text = str(word["w"]).strip()
        if not text:
            continue
        if not current:
            current = [word]
            continue

        candidate_len = sum(len(str(w["w"]).strip()) + 1 for w in current) + len(text)
        gap = float(word["start"]) - float(current[-1]["end"])
        span = float(word["end"]) - float(current[0]["start"])

        if (
            len(current) >= style.max_words
            or candidate_len > style.max_chars
            or gap > style.max_gap_s
            or span > style.max_line_s
        ):
            lines.append(current)
            current = [word]
        else:
            current.append(word)

    if current:
        lines.append(current)
    return lines


def _header(style: CaptionStyle) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_W}
PlayResY: {PLAY_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Clip,{style.font},{style.font_size},{style.primary_colour},{style.highlight_colour},{style.outline_colour},{style.back_colour},{style.bold},0,0,0,100,100,0,0,1,{style.outline},{style.shadow},2,{style.margin_h},{style.margin_h},{style.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(words: list[Word], style: CaptionStyle | None = None) -> str:
    """Render an ASS file where each word lights up as it is spoken.

    One Dialogue event per word (the whole line redrawn with a different word
    highlighted) rather than \\k karaoke tags — it renders identically across
    libass versions and survives words with zero-length timings.
    """
    style = style or CaptionStyle()
    events: list[str] = []
    lines = group_words_into_lines(words, style)

    for line_idx, line in enumerate(lines):
        pieces = [escape_text(w["w"]) for w in line]
        line_end = float(line[-1]["end"]) + style.line_hold_s
        if line_idx + 1 < len(lines):
            # Never overlap the next line, and never sit on screen through a
            # long pause.
            line_end = min(line_end, float(lines[line_idx + 1][0]["start"]))
        line_end = max(line_end, float(line[-1]["end"]))

        for i, word in enumerate(line):
            start = float(word["start"])
            # Hold each word until the next one begins so the line never blinks.
            end = float(line[i + 1]["start"]) if i + 1 < len(line) else line_end
            if end <= start:
                end = start + 0.08

            rendered = " ".join(
                (
                    f"{{\\c{style.highlight_colour}}}{piece}{{\\c{style.primary_colour}}}"
                    if j == i
                    else piece
                )
                for j, piece in enumerate(pieces)
            )
            events.append(
                f"Dialogue: 0,{format_ts(start)},{format_ts(end)},Clip,,0,0,0,,{rendered}"
            )

    return _header(style) + "\n".join(events) + "\n"


def write_ass(words: list[Word], path: Path | str, style: CaptionStyle | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_ass(words, style), encoding="utf-8")
    return path
