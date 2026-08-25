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
    monkeypatch.setattr(settings, "ytdlp_cookies", None, raising=False)
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

    with pytest.raises(ytdlp.BotCheck, match="YTDLP_COOKIES"):
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


def test_cookies_put_the_web_client_first(monkeypatch):
    """Cookies are a web session; a client that ignores them wastes the attempt."""
    monkeypatch.setattr(
        settings, "ytdlp_cookies_b64", base64.b64encode(b"# cookies\n").decode(), raising=False
    )

    assert ytdlp.player_clients()[0] == "web"


def test_without_cookies_the_configured_order_is_untouched():
    assert ytdlp.player_clients() == ["tv", "web_safari"]


def test_an_explicit_web_entry_is_not_duplicated(monkeypatch):
    monkeypatch.setattr(settings, "ytdlp_player_clients", "mweb,web,tv", raising=False)
    monkeypatch.setattr(
        settings, "ytdlp_cookies_b64", base64.b64encode(b"# cookies\n").decode(), raising=False
    )

    assert ytdlp.player_clients().count("web") == 1


TAB_COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1799999999\tSID\tabc123\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1799999999\tHSID\tdef456\n"
)


def test_pasted_cookies_are_used_as_they_come(monkeypatch):
    monkeypatch.setattr(settings, "ytdlp_cookies", TAB_COOKIES, raising=False)

    from pathlib import Path

    written = Path(ytdlp.cookiefile()).read_text()

    assert "SID\tabc123" in written
    assert "HSID\tdef456" in written


def test_tabs_lost_in_a_text_box_are_repaired(monkeypatch):
    """A pasted cookies.txt usually arrives with its tabs turned into spaces."""
    monkeypatch.setattr(settings, "ytdlp_cookies", TAB_COOKIES.replace("\t", "    "), raising=False)

    from pathlib import Path

    written = Path(ytdlp.cookiefile()).read_text()

    assert ".youtube.com\tTRUE\t/\tTRUE\t1799999999\tSID\tabc123" in written


def test_a_value_containing_spaces_survives_the_repair(monkeypatch):
    monkeypatch.setattr(
        settings,
        "ytdlp_cookies",
        ".youtube.com TRUE / TRUE 1799999999 PREF f6=40000000&hl=en gb",
        raising=False,
    )

    from pathlib import Path

    written = Path(ytdlp.cookiefile()).read_text()

    # Six fields, then everything else folded back into the value.
    assert written.splitlines()[1].split("\t")[6] == "f6=40000000&hl=en gb"


def test_cookie_text_with_nothing_usable_is_ignored(monkeypatch):
    monkeypatch.setattr(settings, "ytdlp_cookies", "I pasted the wrong thing", raising=False)

    assert ytdlp.cookiefile() is None


def test_raw_text_wins_over_base64_when_both_are_set(monkeypatch):
    monkeypatch.setattr(settings, "ytdlp_cookies", TAB_COOKIES, raising=False)
    monkeypatch.setattr(
        settings, "ytdlp_cookies_b64", base64.b64encode(b"# other\n").decode(), raising=False
    )

    from pathlib import Path

    assert "abc123" in Path(ytdlp.cookiefile()).read_text()


def test_comment_lines_do_not_count_as_cookies(monkeypatch):
    monkeypatch.setattr(
        settings, "ytdlp_cookies", "# Netscape HTTP Cookie File\n# nothing else\n", raising=False
    )

    assert ytdlp.cookiefile() is None


def test_describe_reports_no_cookies_when_there_are_none():
    payload = ytdlp.describe()

    assert payload["cookies_loaded"] is False
    assert payload["cookies_source"] == "none"
    assert payload["cookie_lines"] == 0
    assert payload["player_clients"] == ["tv", "web_safari"]


def test_describe_counts_the_cookies_it_loaded(monkeypatch):
    monkeypatch.setattr(settings, "ytdlp_cookies", TAB_COOKIES, raising=False)

    payload = ytdlp.describe()

    assert payload["cookies_loaded"] is True
    assert payload["cookies_source"] == "pasted text"
    assert payload["cookie_lines"] == 2
    assert payload["player_clients"][0] == "web"


def test_describe_never_leaks_the_cookies(monkeypatch):
    monkeypatch.setattr(settings, "ytdlp_cookies", TAB_COOKIES, raising=False)
    monkeypatch.setattr(settings, "ytdlp_proxy", "http://user:pw@host:1", raising=False)

    body = repr(ytdlp.describe())

    assert "abc123" not in body
    assert "user:pw" not in body


def test_the_endpoint_flags_a_partial_paste(monkeypatch):
    monkeypatch.setattr(settings, "ytdlp_cookies", TAB_COOKIES, raising=False)

    import api.routes.settings as settings_routes

    payload = settings_routes.ytdlp_status()

    assert "partial" in payload["hint"]


def test_the_endpoint_says_when_nothing_arrived():
    import api.routes.settings as settings_routes

    payload = settings_routes.ytdlp_status()

    assert "No cookies loaded" in payload["hint"]
