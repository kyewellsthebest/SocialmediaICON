from __future__ import annotations

import pytest

from worker.tasks.ingest import LicenseError, check_license


@pytest.mark.parametrize("tag", ["own", "licensed", "campaign", "permitted"])
def test_valid_licenses_pass_in_prod(tag):
    assert check_license(tag, env="prod") == tag


def test_license_none_is_refused_in_prod():
    with pytest.raises(LicenseError, match="license=none"):
        check_license("none", env="prod")


def test_license_none_is_allowed_in_dev_for_testing():
    assert check_license("none", env="dev") == "none"


def test_unknown_license_is_rejected_everywhere():
    with pytest.raises(LicenseError, match="unknown license"):
        check_license("probably-fine", env="dev")


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
