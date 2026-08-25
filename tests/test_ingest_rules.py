from __future__ import annotations

import pytest

from worker.tasks.ingest import check_license


@pytest.mark.parametrize("tag", ["own", "licensed", "campaign", "permitted"])
def test_a_known_tag_is_kept_as_given(tag):
    assert check_license(tag, env="prod") == tag


def test_nothing_is_refused_any_more():
    """The tag is a record, not a gate - ingest never blocks on it."""
    assert check_license("none", env="prod") == "none"


def test_an_unrecognised_tag_falls_back_rather_than_raising():
    assert check_license("probably-fine", env="dev") == "none"


def test_a_missing_tag_is_allowed():
    assert check_license() == "none"
    assert check_license(None) == "none"
    assert check_license("") == "none"


def test_license_is_case_and_space_insensitive():
    assert check_license("  OWN  ", env="prod") == "own"


class TestDownloadResolution:
    """The height cap is the dial between clip sharpness and a metered bill."""

    def _format_for(self, monkeypatch, height=None):
        from pathlib import Path

        import worker.tasks.ingest as ingest

        captured: dict = {}

        class FakeYDL:
            def __init__(self, options):
                captured.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=True):
                return {"id": "abc", "title": "t", "duration": 1, "webpage_url": url}

            def prepare_filename(self, info):
                return "abc.mp4"

        fake = type("M", (), {"YoutubeDL": FakeYDL})
        monkeypatch.setitem(__import__("sys").modules, "yt_dlp", fake)
        monkeypatch.setattr(Path, "exists", lambda self: True)
        ingest.download_source("https://x/1", "/tmp", max_height=height)
        return captured["format"]

    def test_the_configured_height_is_used_by_default(self, monkeypatch):
        from core.config import settings

        monkeypatch.setattr(settings, "ingest_max_height", 720, raising=False)

        assert "height<=720" in self._format_for(monkeypatch)

    def test_an_explicit_height_still_wins(self, monkeypatch):
        from core.config import settings

        monkeypatch.setattr(settings, "ingest_max_height", 720, raising=False)

        assert "height<=480" in self._format_for(monkeypatch, height=480)
