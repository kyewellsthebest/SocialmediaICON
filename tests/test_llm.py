from __future__ import annotations

import pytest

from core.llm import DETECT_SCHEMA, DETECT_USER, extract_json


def test_extract_plain_json():
    assert extract_json('{"candidates": []}') == {"candidates": []}


def test_extract_fenced_json():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_wrapped_in_prose():
    text = 'Here you go:\n{"a": {"b": [1, 2]}}\nHope that helps.'
    assert extract_json(text) == {"a": {"b": [1, 2]}}


def test_braces_inside_strings_do_not_confuse_the_parser():
    text = 'preamble {"reason": "he said {this} and \\"that\\"", "ok": true} trailing'
    assert extract_json(text)["ok"] is True


def test_extract_raises_on_garbage():
    with pytest.raises(ValueError):
        extract_json("no json here at all")
    with pytest.raises(ValueError):
        extract_json('prefix {"a": 1')


def test_detect_prompt_renders_without_stray_braces():
    rendered = DETECT_USER.format(
        niche="fitness", window_start=12.0, words_with_timestamps="[12.0] hello"
    )
    assert '"candidates"' in rendered
    assert "{niche}" not in rendered
    assert rendered.count("{{") == 0


def test_detect_schema_is_strict():
    assert DETECT_SCHEMA["additionalProperties"] is False
    item = DETECT_SCHEMA["properties"]["candidates"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])


def test_metadata_prompt_bans_the_patterns_that_read_as_automated():
    """The caption is the main thing a human sees before the video plays. These
    are the tells that mark an account as a bot farm rather than a person."""
    from core.llm import METADATA_SYSTEM, METADATA_USER

    system = METADATA_SYSTEM.lower()
    for tell in ("wait for it", "follow for more", "comment below", "emoji", "engagement bait"):
        assert tell in system, f"prompt no longer warns against {tell!r}"

    rendered = METADATA_USER.format(niche="metal detecting", clip_text="x", hashtag_count=4)
    assert "#viral" in rendered and "#fyp" in rendered, "should steer away from spam tags"
    assert "4 of them" in rendered


def test_hashtag_count_is_a_handful_not_a_wall():
    from core.config import settings

    assert 3 <= settings.hashtag_count <= 8
