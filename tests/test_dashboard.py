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


class TestTheVideoOverlayCanAlwaysBeClosed:
    """It could not be. `.player` sets `display: flex`, and any author rule
    that sets display beats the browser's own `[hidden] { display: none }` -
    so the overlay was permanently visible, sitting on top of the password
    box, with a Close button that set an attribute nothing was reading.

    Two faults in one: the attribute did nothing, and the handler was bound
    inside start(), which never runs when the token has been refused. So the
    one screen where you most need to dismiss it was the one screen where the
    button had never been wired up.
    """

    CSS = Path(__file__).resolve().parent.parent / "api" / "static" / "app.css"

    def test_hidden_beats_any_display_rule(self):
        css = self.CSS.read_text(encoding="utf-8")
        assert "[hidden] { display: none !important; }" in css

    def test_the_rule_comes_before_the_components_that_need_it(self):
        """Same specificity would otherwise be settled by source order."""
        css = self.CSS.read_text(encoding="utf-8")
        assert css.index("[hidden]") < css.index(".player {")

    def test_closing_is_bound_at_load_not_inside_start(self):
        """start() does not run when the token was refused, which is exactly
        when an overlay stuck over the password box cannot be dismissed."""
        js = APP_JS.read_text(encoding="utf-8")
        start = js.index("function start()")
        binding = js.index('$("player-close").onclick')
        assert binding > js.index("function gate()"), (
            "the close handler must be bound at the top level, not inside start()")
        assert binding > start or "closePlayer" in js[start:binding]

    def test_there_are_three_ways_out(self):
        """A button, the backdrop, and Escape. A modal with one way out is a
        modal that traps someone the moment that one way breaks."""
        js = APP_JS.read_text(encoding="utf-8")
        assert '$("player-close").onclick = closePlayer' in js
        assert 'event.target === $("player")' in js
        assert '"Escape"' in js

    def test_the_gate_dismisses_it(self):
        """A refused token can arrive while the player is open."""
        js = APP_JS.read_text(encoding="utf-8")
        gate = js.split("function gate()")[1].split("\n}")[0]
        assert "closePlayer()" in gate

    def test_closing_actually_stops_the_download(self):
        """Removing src without load() leaves the browser streaming a video
        nobody is watching."""
        js = APP_JS.read_text(encoding="utf-8")
        body = js.split("function closePlayer()")[1].split("\n}")[0]
        assert "pause()" in body and "removeAttribute" in body and "load()" in body


class TestARunThatNeverRuns:
    """There used to be a worker service taking jobs off a Redis queue, and
    it produced a failure with no symptom: the dashboard accepted a run, Redis
    accepted the job, and nothing ever picked it up, because the worker was
    not deployed. "Queued" and "queued and abandoned" look identical.

    The run happens on a thread in this service now, so there is no queue to
    be stuck in and no worker to be missing. What can still go wrong is that
    it throws - and a thread has nobody waiting on it and no response to fail,
    so that has to be written down or it never appears to have happened.
    """

    def test_there_is_no_worker_service_left_to_forget_to_deploy(self):
        from pathlib import Path as P

        procfile = (P(__file__).resolve().parent.parent / "Procfile").read_text()
        assert "worker:" not in procfile
        assert "scheduler:" not in procfile

    def test_starting_the_old_services_says_what_happened_to_them(self):
        """Rather than failing on a missing module, which reads as a broken
        deploy rather than as a service that should be deleted."""
        from pathlib import Path as P

        start = (P(__file__).resolve().parent.parent / "scripts" / "start.sh").read_text()
        assert "there is no '$ROLE' service any more" in start
        assert "Delete this service in Railway" in start

    def test_a_failed_run_is_written_down_with_its_traceback(self):
        import inspect

        from core import jobs
        source = inspect.getsource(jobs.run)
        assert "format_exception" in source
        assert "_record_end(run_id, None, detail)" in source

    def test_two_runs_cannot_overlap(self):
        """A second pass over the same rooms would double every download and
        race the first one on the same rows."""
        from core import jobs

        assert jobs._running.acquire(blocking=False)
        try:
            assert jobs.start_in_background() is False
            assert "already going" in jobs.run()["skipped"]
        finally:
            jobs._running.release()

    def test_the_schedule_is_measured_from_when_a_run_started(self, monkeypatch):
        """Not from when it finished. A pass that takes twenty minutes should
        still go once a day, rather than drifting twenty minutes later every
        day until it is going at midnight."""
        import inspect

        from core import jobs
        assert "started_at" in inspect.getsource(jobs.due)

    def test_there_is_a_way_to_run_and_wait_for_the_answer(self):
        """Because "the harvest is broken" and "nothing is running it" are
        different problems that produce the same empty queue."""
        import inspect

        import api.routes.app as routes
        source = inspect.getsource(routes.run_now)
        assert "inline" in source and "rooms" in source

    def test_the_waiting_version_is_capped_so_a_browser_will_hold_on(self):
        import inspect

        import api.routes.app as routes
        assert "rooms or 3" in inspect.getsource(routes.run_now)

    def test_capping_the_rooms_reaches_the_harvest(self, monkeypatch):
        from core.config import settings as env
        from worker.tasks import harvest

        asked = []
        monkeypatch.setattr(env, "reddit_subreddits", "a,b,c,d,e")
        monkeypatch.setattr(harvest, "discover", lambda only=None: asked.append(only) or [])
        monkeypatch.setattr(harvest, "admit", lambda posts: 0)
        monkeypatch.setattr(harvest, "trim", lambda: 0)
        monkeypatch.setattr(harvest, "queued", lambda limit=None: [])

        harvest.run(post=False, rooms=3)
        assert asked == [["a", "b", "c"]]

    def test_a_full_run_reads_every_room(self, monkeypatch):
        from worker.tasks import harvest

        asked = []
        monkeypatch.setattr(harvest, "discover", lambda only=None: asked.append(only) or [])
        monkeypatch.setattr(harvest, "admit", lambda posts: 0)
        monkeypatch.setattr(harvest, "trim", lambda: 0)
        monkeypatch.setattr(harvest, "queued", lambda limit=None: [])

        harvest.run(post=False)
        assert asked == [None], "an uncapped run must not silently read three"

    def test_the_meta_tokens_are_still_refreshed(self):
        """They die at sixty days and cannot be revived. Losing that job when
        the worker service went would be a silent sixty-day fuse."""
        import inspect

        from core import jobs
        assert "refresh_tokens" in inspect.getsource(jobs._maybe_refresh_tokens)


class TestTheDashboardTalksToElementsThatExist:
    """`$("thing")` on an id the page does not carry returns null, and the
    next line is a TypeError - at runtime, on whichever path touches it, which
    may be one nobody opens for a week. The eslint test that would catch the
    scope errors skips when eslint is absent; this needs nothing.
    """

    PAGE = Path(__file__).resolve().parent.parent / "api" / "static" / "index.html"

    def _ids(self) -> set[str]:
        return set(re.findall(r'id="([^"]+)"', self.PAGE.read_text(encoding="utf-8")))

    def _used(self) -> set[str]:
        return set(re.findall(r'\$\("([^"]+)"\)', APP_JS.read_text(encoding="utf-8")))

    def test_every_element_the_script_reaches_for_is_on_the_page(self):
        missing = sorted(self._used() - self._ids())
        assert not missing, f"the script asks for ids the page does not have: {missing}"

    def test_the_check_is_actually_finding_things(self):
        """A regex that matched nothing would pass the test above while
        checking nothing at all."""
        assert len(self._used()) > 10
        assert "run-now" in self._used()

    def test_the_script_parses(self):
        """Caught by nothing else here: a syntax error anywhere in the file
        means the whole dashboard is a blank page."""
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")
        done = subprocess.run(  # noqa: S603
            [node, "--check", str(APP_JS)], capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
