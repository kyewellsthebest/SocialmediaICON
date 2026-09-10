"""Two failures that only showed up once it was deployed.

Neither was subtle in hindsight and neither was reachable from a unit test of
the thing itself, because both were about how the pieces are arranged rather
than about what any one of them does.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.main as main
from core.config import settings

APP_JS = Path(__file__).resolve().parent.parent / "api" / "static" / "app.js"


class TestThePageDoesNotReloadItselfForever:
    """The first thing the page does on load is call /overview, to find out
    whether the stored token still works. Reloading the page when that comes
    back 401 is a loop: load, ask, get refused, reload, ask again - several
    times a second, forever, against a server answering exactly correctly.

    It showed up in the deploy log as alternating `GET / 200` and
    `GET /api/overview 401`, dozens of times, which reads as an attack rather
    than as a page failing to show a password box.
    """

    def test_nothing_reloads_the_page(self):
        assert "location.reload" not in APP_JS.read_text(encoding="utf-8")

    def test_a_refusal_shows_the_gate_instead(self):
        body = APP_JS.read_text(encoding="utf-8")
        after = body.split("response.status === 401")[1].split("}")[0]
        assert "gate()" in after
        assert "removeItem" in after, "a token that was refused must not be kept"

    def test_the_gate_hides_the_app_behind_it(self):
        """Otherwise the refusal leaves a half-drawn dashboard underneath a
        password box, which looks like it half worked."""
        body = APP_JS.read_text(encoding="utf-8")
        gate = body.split("function gate()")[1].split("function ")[0]
        assert '$("app").hidden = true' in gate

    def test_the_first_call_swallows_its_failure(self):
        """It is a probe, not a request anyone made. There is nothing to
        report and nothing to retry against."""
        body = APP_JS.read_text(encoding="utf-8")
        last = body.strip().splitlines()[-1]
        assert 'api("/overview")' in last
        assert re.search(r"catch\(\(\)\s*=>\s*\{\s*\}\)", last), last


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


class TestTheVideoIsNotOnOneContainersDisk:
    """web, worker and scheduler are three separate Railway services with
    three separate filesystems. The worker downloads and brands the file and
    writes its path to the database; the dashboard then looks for that path on
    its *own* disk, finds nothing, and reports a video that was never
    downloaded. Storage is the only place all three can see.
    """

    def test_the_branded_file_goes_into_storage(self):
        import inspect

        from worker.tasks import harvest
        source = inspect.getsource(harvest.prepare)
        assert "put_file" in source, "a file only on this disk is invisible elsewhere"
        assert "storage_key" in source

    def test_a_storage_failure_does_not_lose_the_reel(self):
        """The local copy still posts. Losing the download because the upload
        failed would turn a warning into a wasted run."""
        import inspect

        from worker.tasks import harvest
        source = inspect.getsource(harvest.prepare)
        assert "could not store" in source
        assert "local_path" in source

    def test_the_dashboard_reads_storage_before_disk(self):
        import ast
        import inspect

        import api.routes.app as routes
        # The body only. The docstring explains the disk case first, and
        # measuring the prose rather than the code is how a test like this
        # quietly stops checking anything.
        tree = ast.parse(inspect.getsource(routes.video).strip())
        body = ast.get_source_segment(
            inspect.getsource(routes.video).strip(),
            tree.body[0]) or ""
        body = "\n".join(
            line for line in body.splitlines()
            if not line.strip().startswith("#")
        )
        body = body.split('"""')[-1]
        assert body.index("storage_key") < body.index("local_path"), (
            "disk first would 404 on the files the worker prepared")

    def test_it_redirects_rather_than_relaying_the_bytes(self):
        """So the browser asks R2 for byte ranges directly - which is what
        makes scrubbing work - and the web service does not spend its memory
        being a video proxy."""
        import inspect

        import api.routes.app as routes
        assert "RedirectResponse" in inspect.getsource(routes.video)

    def test_a_reel_counts_as_ready_if_a_file_exists_anywhere(self):
        import inspect

        import api.routes.app as routes
        source = inspect.getsource(routes._reel)
        line = next(ln for ln in source.splitlines() if '"ready"' in ln)
        assert "storage_key" in line and "local_path" in line

    def test_the_missing_file_message_explains_which_case_it_is(self, monkeypatch):
        """"Not found" for a file that exists on another container is the
        message that sent me looking in the wrong place."""
        import inspect

        import api.routes.app as routes
        source = inspect.getsource(routes.video)
        assert "worker's disk" in source


class TestTheTokenGateStillHolds:
    def test_the_video_route_is_not_a_way_round_the_token(self, client, monkeypatch):
        monkeypatch.setattr(settings, "dashboard_token", "sekrit")
        assert client.get("/api/reels/1/video").status_code == 401

    def test_a_query_token_works_because_a_video_tag_cannot_send_headers(
            self, client, monkeypatch):
        """The player sets src="…?token=…" - there is no way to attach a
        header to a <video> src, so the query string has to be accepted."""
        monkeypatch.setattr(settings, "dashboard_token", "sekrit")
        monkeypatch.setattr(settings, "database_url", None)
        # 503 rather than 401: past the gate, and failing on the database it
        # has not got, which is the correct next failure.
        assert client.get("/api/reels/1/video?token=sekrit").status_code == 503
