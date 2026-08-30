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


class TestEverySchemaTheApiWillAccept:
    """Structured outputs reject several JSON Schema keywords outright, with a
    400 that fails the whole call rather than the one field.

    This cost a night of clipping. `confidence` carried `minimum`/`maximum`,
    so every research call and every verdict returned "For 'number' type,
    properties maximum, minimum are not supported" - which meant no streamer
    could be shown to be English and non-gaming, which meant nothing was
    eligible, which meant nothing was watched. Ranges go in descriptions and
    are clamped on the way in.
    """

    #: From the structured-outputs documentation, not from guessing.
    REJECTED = {
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "multipleOf", "minLength", "maxLength", "minItems", "maxItems",
        "uniqueItems", "pattern",
    }

    def schemas(self):
        from core import profile, verdict

        return {"profile.SCHEMA": profile.SCHEMA, "verdict.SCHEMA": verdict.SCHEMA}

    def walk(self, node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                yield path, key
                yield from self.walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                yield from self.walk(value, f"{path}[{i}]")

    def test_no_schema_uses_a_keyword_the_endpoint_rejects(self):
        for name, schema in self.schemas().items():
            for path, key in self.walk(schema, name):
                assert key not in self.REJECTED, f"{path}.{key} is rejected by the API"

    def test_every_object_closes_itself(self):
        """additionalProperties must be present and false on every object -
        anything else is rejected too."""
        for name, schema in self.schemas().items():
            for path, node in self.objects(schema, name):
                assert node.get("additionalProperties") is False, f"{path} is open"

    def objects(self, node, path=""):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield path, node
            for key, value in node.items():
                yield from self.objects(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                yield from self.objects(value, f"{path}[{i}]")
