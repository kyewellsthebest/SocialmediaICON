"""Where a reel goes, and why nobody types it in.

There used to be a table of handles to fill in by hand, and it was the thing
stopping anything from posting: a full set of Meta credentials sat in the
environment, naming every account perfectly well, next to an empty table
saying there was nowhere to post to.

Two records of one fact is one too many. INSTAGRAM_USER_ID *is* the Instagram
account. A handle typed beside it adds nothing and can only ever disagree with
it - and the way it disagrees is by naming an account the credentials cannot
reach, which reads as "the post failed" rather than "those are two different
accounts".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import settings
from core.publishers import destinations


@pytest.fixture(autouse=True)
def _blank(monkeypatch):
    """Nothing configured, so each test states exactly what it needs."""
    for name in ("meta_access_token", "instagram_user_id", "instagram_access_token",
                 "threads_user_id", "threads_access_token", "facebook_page_id",
                 "facebook_page_token", "youtube_client_id", "youtube_client_secret",
                 "youtube_refresh_token", "upload_post_api_key", "upload_post_user"):
        monkeypatch.setattr(settings, name, None)
    monkeypatch.setattr(settings, "publisher", "meta")


class TestTheCredentialsAreTheAccountList:
    def test_an_instagram_id_and_token_is_an_instagram_destination(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_user_id", "17841400000000000")
        monkeypatch.setattr(settings, "meta_access_token", "EAA...")
        assert destinations() == ["instagram"]

    def test_all_three_meta_platforms_at_once(self, monkeypatch):
        monkeypatch.setattr(settings, "meta_access_token", "EAA...")
        monkeypatch.setattr(settings, "instagram_user_id", "1")
        monkeypatch.setattr(settings, "threads_user_id", "2")
        monkeypatch.setattr(settings, "threads_access_token", "t")
        monkeypatch.setattr(settings, "facebook_page_id", "3")
        assert destinations() == ["instagram", "threads", "facebook"]

    def test_an_id_with_no_token_is_not_a_destination(self, monkeypatch):
        """It cannot be posted to, so listing it would promise something the
        run cannot deliver."""
        monkeypatch.setattr(settings, "instagram_user_id", "1")
        assert destinations() == []

    def test_a_token_with_no_id_is_not_a_destination_either(self, monkeypatch):
        monkeypatch.setattr(settings, "meta_access_token", "EAA...")
        assert destinations() == []

    def test_nothing_configured_is_an_empty_list_not_a_guess(self):
        assert destinations() == []


class TestEachPublisherReachesItsOwn:
    def test_youtube_needs_the_whole_oauth_triple(self, monkeypatch):
        monkeypatch.setattr(settings, "publisher", "youtube")
        monkeypatch.setattr(settings, "youtube_client_id", "a")
        monkeypatch.setattr(settings, "youtube_client_secret", "b")
        assert destinations() == [], "two of three is not a working credential"
        monkeypatch.setattr(settings, "youtube_refresh_token", "c")
        assert destinations() == ["youtube"]

    def test_meta_credentials_do_not_leak_into_the_youtube_publisher(self, monkeypatch):
        """A key being set is not the same as it being the one in use."""
        monkeypatch.setattr(settings, "publisher", "youtube")
        monkeypatch.setattr(settings, "meta_access_token", "EAA...")
        monkeypatch.setattr(settings, "instagram_user_id", "1")
        assert destinations() == []

    def test_the_reseller_is_told_where_to_post_rather_than_asked(self, monkeypatch):
        """One key reaches a dozen platforms, so which ones is a decision
        rather than something the credentials can answer."""
        monkeypatch.setattr(settings, "publisher", "upload_post")
        monkeypatch.setattr(settings, "upload_post_api_key", "k")
        monkeypatch.setattr(settings, "upload_post_user", "u")
        monkeypatch.setattr(settings, "upload_post_platforms", "tiktok,snapchat")
        assert destinations() == ["tiktok", "snapchat"]

    def test_a_platform_the_reseller_does_not_have_is_dropped(self, monkeypatch):
        monkeypatch.setattr(settings, "publisher", "upload_post")
        monkeypatch.setattr(settings, "upload_post_api_key", "k")
        monkeypatch.setattr(settings, "upload_post_user", "u")
        monkeypatch.setattr(settings, "upload_post_platforms", "tiktok,myspace")
        assert destinations() == ["tiktok"]

    def test_snapchat_is_reachable_because_it_is_the_reason_to_use_one(self):
        """There is no practical public posting API for Snapchat, and
        TikTok's own one only posts drafts until an app has been audited."""
        from core.publishers.upload_post import PLATFORM_NAMES

        assert "snapchat" in PLATFORM_NAMES

    def test_manual_reports_what_would_be_reachable(self, monkeypatch):
        """"Nothing configured" and "configured but switched off" are
        different problems and must not read the same."""
        monkeypatch.setattr(settings, "publisher", "manual")
        monkeypatch.setattr(settings, "meta_access_token", "EAA...")
        monkeypatch.setattr(settings, "instagram_user_id", "1")
        assert destinations() == ["instagram"]


class TestNothingAsksForAHandle:
    def test_there_is_no_accounts_table(self):
        import core.models as models

        assert not hasattr(models, "Account")

    def test_publishing_asks_the_credentials_not_the_database(self):
        import inspect

        from worker.tasks import publish
        source = inspect.getsource(publish.publish_one)
        assert "destinations()" in source
        assert "Account" not in inspect.getsource(publish)

    def test_the_dashboard_has_nowhere_to_type_one(self):
        page = (Path(__file__).resolve().parent.parent
                / "api" / "static" / "index.html").read_text(encoding="utf-8")
        assert "acc-handle" not in page
        assert "destinations" in page
