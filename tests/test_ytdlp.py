"""yt-dlp configuration and the bot-check fallback.

The failure this handles is not a bug in the request - YouTube challenges
whole IP ranges - so the tests care that we try the alternatives and then say
something useful rather than repeating a raw yt-dlp string at the user.
"""

from __future__ import annotations

import base64

import pytest

from core import ytdlp
from core.config import settings


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(ytdlp, "_cookiefile", None, raising=False)
    monkeypatch.setattr(settings, "ytdlp_cookies_b64", None, raising=False)
    monkeypatch.setattr(settings, "ytdlp_cookiefile", None, raising=False)
    monkeypatch.setattr(settings, "ytdlp_proxy", None, raising=False)
    monkeypatch.setattr(settings, "ytdlp_player_clients", "tv,web_safari", raising=False)


def _bot_check() -> Exception:
    return RuntimeError("ERROR: [youtube] abc: Sign in to confirm you're not a bot.")


def test_a_challenged_client_falls_through_to_the_next():
    tried: list[str] = []

    def call(options: dict) -> str:
        client = options["extractor_args"]["youtube"]["player_client"][0]
        tried.append(client)
        if client == "tv":
            raise _bot_check()
        return "downloaded"

    assert ytdlp.run(call, ytdlp.base_options()) == "downloaded"
    assert tried == ["tv", "web_safari"]


def test_every_client_challenged_raises_something_actionable():
    def call(options: dict) -> str:
        raise _bot_check()

    with pytest.raises(ytdlp.BotCheck, match="YTDLP_COOKIES_B64"):
        ytdlp.run(call, ytdlp.base_options())


def test_a_real_error_is_raised_at_once_not_retried():
    calls: list[str] = []

    def call(options: dict) -> str:
        calls.append("x")
        raise ValueError("Video unavailable")

    with pytest.raises(ValueError, match="unavailable"):
        ytdlp.run(call, ytdlp.base_options())
    assert len(calls) == 1  # no point asking three clients about a dead video


def test_the_first_client_wins_when_it_works():
    tried: list[str] = []

    def call(options: dict) -> str:
        tried.append(options["extractor_args"]["youtube"]["player_client"][0])
        return "ok"

    ytdlp.run(call, ytdlp.base_options())
    assert tried == ["tv"]


def test_cookies_travel_as_base64_and_land_in_a_private_file(monkeypatch):
    payload = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tX\ty\n"
    monkeypatch.setattr(
        settings, "ytdlp_cookies_b64", base64.b64encode(payload).decode(), raising=False
    )

    path = ytdlp.cookiefile()

    assert path is not None
    from pathlib import Path

    written = Path(path)
    assert written.read_bytes() == payload
    assert written.stat().st_mode & 0o077 == 0  # not readable by anyone else
    assert ytdlp.base_options()["cookiefile"] == path


def test_malformed_cookies_are_ignored_rather_than_crashing(monkeypatch):
    monkeypatch.setattr(settings, "ytdlp_cookies_b64", "not base64 at all!", raising=False)

    assert ytdlp.cookiefile() is None
    assert "cookiefile" not in ytdlp.base_options()


def test_a_proxy_is_passed_through(monkeypatch):
    monkeypatch.setattr(settings, "ytdlp_proxy", "http://user:pw@proxy:8080", raising=False)

    assert ytdlp.base_options()["proxy"] == "http://user:pw@proxy:8080"


def test_overrides_win_over_the_defaults():
    options = ytdlp.base_options(retries=9, format="bestaudio")

    assert options["retries"] == 9
    assert options["format"] == "bestaudio"
    assert options["quiet"] is True


def test_bot_check_detection_is_not_fooled_by_ordinary_errors():
    assert ytdlp.is_bot_check(_bot_check())
    assert not ytdlp.is_bot_check(RuntimeError("HTTP Error 404: Not Found"))
