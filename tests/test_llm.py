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
