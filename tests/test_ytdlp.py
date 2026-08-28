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


def test_the_failure_says_what_this_worker_actually_had(monkeypatch):
    """The service that failed is the only one whose config matters."""
    monkeypatch.setattr(settings, "ytdlp_cookies", TAB_COOKIES, raising=False)
    monkeypatch.setattr(settings, "ytdlp_proxy", "http://p:1", raising=False)

    def call(options: dict) -> str:
        raise _bot_check()

    with pytest.raises(ytdlp.BotCheck) as caught:
        ytdlp.run(call, ytdlp.base_options())

    message = str(caught.value)
    assert "cookies=yes(2)" in message
    assert "tried=web,tv,web_safari" in message
    assert message.startswith("[proxies=1of1")


def test_the_failure_calls_out_missing_cookies_loudly():
    def call(options: dict) -> str:
        raise _bot_check()

    with pytest.raises(ytdlp.BotCheck) as caught:
        ytdlp.run(call, ytdlp.base_options())

    assert "cookies=NO" in str(caught.value)
    assert str(caught.value).startswith("[proxies=1of1")


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


def _no_format() -> Exception:
    return RuntimeError(
        "ERROR: [youtube] Ssr9PQ87TEU: Requested format is not available. "
        "Use --list-formats for a list of available formats"
    )


def test_a_client_with_no_usable_format_falls_through(monkeypatch):
    """The web client authenticates and then offers nothing downloadable."""
    monkeypatch.setattr(settings, "ytdlp_cookies", TAB_COOKIES, raising=False)
    tried: list[str] = []

    def call(options: dict) -> str:
        client = options["extractor_args"]["youtube"]["player_client"][0]
        tried.append(client)
        if client == "web":
            raise _no_format()
        return "downloaded"

    assert ytdlp.run(call, ytdlp.base_options()) == "downloaded"
    assert tried == ["web", "tv"]


def test_a_bot_check_then_a_format_problem_both_fall_through():
    order = iter([_bot_check(), _no_format()])
    tried: list[str] = []

    def call(options: dict) -> str:
        tried.append(options["extractor_args"]["youtube"]["player_client"][0])
        raise next(order)

    with pytest.raises(ytdlp.BotCheck):
        ytdlp.run(call, ytdlp.base_options())
    assert tried == ["tv", "web_safari"]


def test_exhausting_clients_on_formats_says_that_not_bot_check():
    def call(options: dict) -> str:
        raise _no_format()

    with pytest.raises(ytdlp.BotCheck) as caught:
        ytdlp.run(call, ytdlp.base_options())

    message = str(caught.value)
    assert "downloadable format" in message
    assert "YTDLP_COOKIES" not in message  # cookies are not the problem here


def test_format_detection_does_not_catch_unrelated_errors():
    assert ytdlp.is_no_usable_format(_no_format())
    assert not ytdlp.is_no_usable_format(RuntimeError("Video unavailable"))
    assert not ytdlp.is_worth_another_client(RuntimeError("Private video"))


def test_missing_pot_can_be_turned_on(monkeypatch):
    monkeypatch.setattr(settings, "ytdlp_allow_missing_pot", True, raising=False)
    seen: list[dict] = []

    def call(options: dict) -> str:
        seen.append(options["extractor_args"]["youtube"])
        return "ok"

    ytdlp.run(call, ytdlp.base_options())

    assert seen[0]["formats"] == ["missing_pot"]


def test_missing_pot_stays_off_when_not_asked_for(monkeypatch):
    monkeypatch.setattr(settings, "ytdlp_allow_missing_pot", False, raising=False)
    seen: list[dict] = []

    def call(options: dict) -> str:
        seen.append(options["extractor_args"]["youtube"])
        return "ok"

    ytdlp.run(call, ytdlp.base_options())

    assert "formats" not in seen[0]


def test_cookies_working_but_ip_blocked_names_the_real_fork(monkeypatch):
    """Telling someone to check cookies when cookies are fine wastes their time."""
    monkeypatch.setattr(settings, "ytdlp_cookies", TAB_COOKIES, raising=False)
    order = iter([_no_format(), _bot_check(), _bot_check()])

    def call(options: dict) -> str:
        raise next(order)

    with pytest.raises(ytdlp.BotCheck) as caught:
        ytdlp.run(call, ytdlp.base_options())

    message = str(caught.value)
    assert message.startswith("[proxies=1of1 cookies=yes")
    assert "residential proxy" in message


def test_a_reload_demand_falls_through_to_the_next_client():
    """YouTube refuses each client differently; only the video is universal."""
    tried: list[str] = []

    def call(options: dict) -> str:
        client = options["extractor_args"]["youtube"]["player_client"][0]
        tried.append(client)
        if client == "tv":
            raise RuntimeError("ERROR: [youtube] 0TRbtFhb0_c: The page needs to be reloaded.")
        return "downloaded"

    assert ytdlp.run(call, ytdlp.base_options()) == "downloaded"
    assert tried == ["tv", "web_safari"]


@pytest.mark.parametrize(
    "message",
    [
        "The page needs to be reloaded.",
        "Unable to extract player response",
        "Failed to extract any player response",
        "Please sign in",
    ],
)
def test_known_client_refusals_are_all_retriable(message):
    assert ytdlp.is_worth_another_client(RuntimeError(message))


@pytest.mark.parametrize(
    "message",
    [
        "Video unavailable",
        "Private video. Sign in if you've been granted access to this video",
        "This video is available to this channel's members",
        "The uploader has not made this video available in your country",
    ],
)
def test_problems_with_the_video_itself_are_not_retried(message):
    """A deleted video fails the same way on every client; retrying is noise."""
    calls: list[str] = []

    def call(options: dict) -> str:
        calls.append("x")
        raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        ytdlp.run(call, ytdlp.base_options())
    assert len(calls) == 1


def test_a_403_on_the_media_falls_through_to_the_next_client():
    """A chosen format that will not fetch is this client's problem, not the video's."""
    tried: list[str] = []

    def call(options: dict) -> str:
        client = options["extractor_args"]["youtube"]["player_client"][0]
        tried.append(client)
        if client == "tv":
            raise RuntimeError("ERROR: unable to download video data: HTTP Error 403: Forbidden")
        return "downloaded"

    assert ytdlp.run(call, ytdlp.base_options()) == "downloaded"
    assert tried == ["tv", "web_safari"]


def test_missing_pot_formats_are_off_by_default():
    """They can be selected and then 403, which wastes the attempt."""
    seen: list[dict] = []

    def call(options: dict) -> str:
        seen.append(options["extractor_args"]["youtube"])
        return "ok"

    ytdlp.run(call, ytdlp.base_options())

    assert "formats" not in seen[0]


class TestProxyPool:
    """Proxies are sold in blocks and shared, so one being burned says nothing
    about the next. Testing one and concluding is how a paid block gets wasted."""

    def test_a_dashboard_export_line_is_understood(self, monkeypatch):
        monkeypatch.setattr(
            settings, "ytdlp_proxies", "31.59.20.176:6754:bob:secret", raising=False
        )

        assert ytdlp.proxies() == ["http://bob:secret@31.59.20.176:6754"]

    def test_full_urls_pass_through_untouched(self, monkeypatch):
        monkeypatch.setattr(
            settings, "ytdlp_proxies", "http://u:p@host:1, socks5://other:2", raising=False
        )

        assert ytdlp.proxies() == ["http://u:p@host:1", "socks5://other:2"]

    def test_newlines_separate_as_well_as_commas(self, monkeypatch):
        monkeypatch.setattr(
            settings, "ytdlp_proxies", "1.1.1.1:80:a:b\n2.2.2.2:81:c:d\n", raising=False
        )

        assert len(ytdlp.proxies()) == 2

    def test_an_unreadable_line_is_skipped_not_fatal(self, monkeypatch):
        monkeypatch.setattr(
            settings, "ytdlp_proxies", "1.1.1.1:80:a:b, total nonsense", raising=False
        )

        assert ytdlp.proxies() == ["http://a:b@1.1.1.1:80"]

    def test_the_single_proxy_setting_still_works(self, monkeypatch):
        monkeypatch.setattr(settings, "ytdlp_proxies", None, raising=False)
        monkeypatch.setattr(settings, "ytdlp_proxy", "http://solo:1", raising=False)

        assert ytdlp.proxies() == ["http://solo:1"]

    def test_a_burned_proxy_moves_on_to_the_next(self, monkeypatch):
        monkeypatch.setattr(
            settings, "ytdlp_proxies", "1.1.1.1:80:a:b,2.2.2.2:80:c:d", raising=False
        )
        monkeypatch.setattr(settings, "ytdlp_max_proxies_per_run", 4, raising=False)
        seen: list[str] = []

        def call(options: dict) -> str:
            seen.append(options["proxy"])
            if "1.1.1.1" in options["proxy"]:
                raise _bot_check()
            return "downloaded"

        assert ytdlp.run(call, ytdlp.base_options()) == "downloaded"
        assert "2.2.2.2" in seen[-1]

    def test_only_the_budgeted_number_of_proxies_is_tried(self, monkeypatch):
        pool = ",".join(f"10.0.0.{i}:80:u:p" for i in range(1, 11))
        monkeypatch.setattr(settings, "ytdlp_proxies", pool, raising=False)
        monkeypatch.setattr(settings, "ytdlp_max_proxies_per_run", 3, raising=False)
        used: set[str] = set()

        def call(options: dict) -> str:
            used.add(options["proxy"])
            raise _bot_check()

        with pytest.raises(ytdlp.BotCheck) as caught:
            ytdlp.run(call, ytdlp.base_options())

        assert len(used) == 3
        assert "3 of your 10 proxies" in str(caught.value)
        assert "untried" in str(caught.value)

    def test_the_whole_pool_being_refused_reads_differently(self, monkeypatch):
        monkeypatch.setattr(settings, "ytdlp_proxies", "1.1.1.1:80:a:b", raising=False)

        def call(options: dict) -> str:
            raise _bot_check()

        with pytest.raises(ytdlp.BotCheck) as caught:
            ytdlp.run(call, ytdlp.base_options())

        assert "untried" not in str(caught.value)

    def test_a_real_error_still_raises_without_burning_the_pool(self, monkeypatch):
        pool = ",".join(f"10.0.0.{i}:80:u:p" for i in range(1, 6))
        monkeypatch.setattr(settings, "ytdlp_proxies", pool, raising=False)
        calls: list[str] = []

        def call(options: dict) -> str:
            calls.append("x")
            raise RuntimeError("Video unavailable")

        with pytest.raises(RuntimeError, match="unavailable"):
            ytdlp.run(call, ytdlp.base_options())
        assert len(calls) == 1


class TestGeoBlocking:
    """ "Not available" is about where the request came from, not the video.
    Asking four more clients through the same exit gets the same answer."""

    def test_the_curly_apostrophe_youtube_actually_sends_is_matched(self):
        assert ytdlp.is_geo_blocked(
            RuntimeError("ERROR: [youtube] Cce_w8qFjoQ: This content isn’t available.")
        )

    def test_the_plain_apostrophe_works_too(self):
        assert ytdlp.is_geo_blocked(RuntimeError("This content isn't available."))

    def test_country_blocks_are_recognised(self):
        assert ytdlp.is_geo_blocked(
            RuntimeError("The uploader has not made this video available in your country")
        )

    def test_an_ordinary_dead_video_is_not_mistaken_for_geo(self):
        assert not ytdlp.is_geo_blocked(RuntimeError("Video unavailable"))
        assert not ytdlp.is_geo_blocked(RuntimeError("Private video"))

    def test_a_geo_block_changes_country_instead_of_client(self, monkeypatch):
        monkeypatch.setattr(
            settings, "ytdlp_proxies", "1.1.1.1:80:a:b,2.2.2.2:80:c:d", raising=False
        )
        monkeypatch.setattr(settings, "ytdlp_max_proxies_per_run", 4, raising=False)
        # The starting proxy advances with the clock; pin it so the assertion
        # is about the fallback and not about what time the suite ran.
        monkeypatch.setattr(ytdlp.time, "time", lambda: 0.0)
        attempts: list[tuple[str, str]] = []

        def call(options: dict) -> str:
            proxy = options["proxy"]
            client = options["extractor_args"]["youtube"]["player_client"][0]
            attempts.append((proxy, client))
            if "1.1.1.1" in proxy:
                raise RuntimeError("This content isn’t available.")
            return "downloaded"

        assert ytdlp.run(call, ytdlp.base_options()) == "downloaded"
        # One attempt on the blocked exit, not four.
        assert len([a for a in attempts if "1.1.1.1" in a[0]]) == 1

    def test_every_exit_blocked_says_region_lock_not_bot_check(self, monkeypatch):
        monkeypatch.setattr(settings, "ytdlp_proxies", "1.1.1.1:80:a:b", raising=False)

        def call(options: dict) -> str:
            raise RuntimeError("This content isn’t available.")

        with pytest.raises(ytdlp.BotCheck) as caught:
            ytdlp.run(call, ytdlp.base_options())

        message = str(caught.value)
        assert "region locked" in message
        assert "cookies" not in message.split("]")[1].lower()


class TestImpersonation:
    """Kick is unreachable without a browser TLS fingerprint.

    yt-dlp's Kick extractor passes impersonate=True on every request, so when
    curl_cffi is missing the request is never made and Cloudflare's 403 gets
    blamed on Kick. That misreading cost this project a source, so the state
    is reported rather than assumed.
    """

    def test_reports_the_targets_that_are_actually_available(self):
        state = ytdlp.impersonation()
        assert state["available"] is True, (
            "curl_cffi should be installed - it is a hard dependency for Kick"
        )
        assert state["targets"] > 0
        assert state["reason"] == ""

    def test_missing_support_is_reported_not_raised(self, monkeypatch):
        class Broken:
            def __enter__(self):
                raise ImportError("no curl_cffi")

            def __exit__(self, *_):
                return False

        import yt_dlp

        monkeypatch.setattr(yt_dlp, "YoutubeDL", lambda *a, **k: Broken())
        state = ytdlp.impersonation()
        assert state["available"] is False
        assert state["targets"] == 0
        assert "curl_cffi" in state["reason"] or "ImportError" in state["reason"]

    def test_describe_carries_it_to_the_dashboard(self):
        assert "impersonation" in ytdlp.describe()
