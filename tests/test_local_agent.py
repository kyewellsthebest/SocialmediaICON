"""The local download agent.

It runs unattended on someone's desktop, so the tests care most about what
happens when things go wrong: one bad source must not stop the loop, and a
crash must not leave the queue permanently claimed.
"""

from __future__ import annotations

import httpx
import pytest

from scripts.local_agent import Agent


def _agent(handler, **kw) -> Agent:
    kw.setdefault("min_gap_s", 0)
    agent = Agent("https://example.invalid", "tok", 1080, keep=False, **kw)
    agent.client = httpx.Client(
        transport=httpx.MockTransport(handler), headers={"X-Dashboard-Token": "tok"}
    )
    return agent


def test_uploads_are_skipped_since_they_already_have_their_file():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": 1, "url": "https://youtu.be/a", "kind": "youtube"},
                {"id": 2, "url": "upload://x.mp4", "kind": "upload"},
            ],
        )

    assert [s["id"] for s in _agent(handler).waiting()] == [1]


def test_the_dashboard_token_is_sent():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-dashboard-token", ""))
        return httpx.Response(200, json=[])

    _agent(handler).waiting()

    assert seen == ["tok"]


def test_one_failing_source_does_not_stop_the_others(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "url": "https://youtu.be/bad", "kind": "youtube"},
                    {"id": 2, "url": "https://youtu.be/good", "kind": "youtube"},
                ],
            )
        return httpx.Response(200, json={"id": 2})

    agent = _agent(handler)
    handled: list[int] = []

    def fetch(url, dest_dir):
        if "bad" in url:
            raise RuntimeError("Video unavailable")
        path = tmp_path / "good.mp4"
        path.write_bytes(b"\x00" * 10)
        return path

    monkeypatch.setattr(agent, "fetch", fetch)
    monkeypatch.setattr(agent, "send", lambda sid, path: handled.append(sid))

    assert agent.tick() == 1
    assert handled == [2]


def test_an_unreachable_deployment_is_survived_not_crashed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    assert _agent(handler).tick() == 0


def test_a_rejected_upload_is_reported_with_the_reason(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="that source already has a video")

    path = tmp_path / "v.mp4"
    path.write_bytes(b"\x00")

    with pytest.raises(RuntimeError, match="already has a video"):
        _agent(handler).send(5, path)


def test_stopping_mid_batch_leaves_the_rest_queued(monkeypatch, tmp_path):
    """Ctrl-C should finish the current source, not abandon the run halfway."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "url": "https://youtu.be/a", "kind": "youtube"},
                    {"id": 2, "url": "https://youtu.be/b", "kind": "youtube"},
                ],
            )
        return httpx.Response(200, json={})

    agent = _agent(handler)
    seen: list[int] = []

    def handle(source):
        seen.append(source["id"])
        agent.stop()

    monkeypatch.setattr(agent, "handle", handle)

    agent.tick()

    assert seen == [1]  # the second is left for the next run


class TestPacing:
    """The connection is shared with the rest of the house, so the traffic
    should look like someone watching videos, not like a scraper."""

    def _idle(self, handler):
        return _agent(handler)

    def test_a_gap_is_left_between_downloads(self):
        slept: list[float] = []
        agent = _agent(lambda r: httpx.Response(200, json=[]), min_gap_s=90)
        agent.finished_at = [__import__("time").monotonic()]

        assert agent.wait_for_slot(sleep=slept.append) is True
        assert slept and 85 <= slept[0] <= 90

    def test_no_wait_when_nothing_has_run_yet(self):
        slept: list[float] = []
        agent = _agent(lambda r: httpx.Response(200, json=[]), min_gap_s=90)

        assert agent.wait_for_slot(sleep=slept.append) is True
        assert slept == []

    def test_the_hourly_cap_stops_the_batch_rather_than_stalling_it(self):
        import time as _t

        agent = _agent(lambda r: httpx.Response(200, json=[]), max_per_hour=3)
        agent.finished_at = [_t.monotonic()] * 3

        assert agent.wait_for_slot(sleep=lambda s: None) is False

    def test_old_downloads_fall_out_of_the_hour(self):
        import time as _t

        agent = _agent(lambda r: httpx.Response(200, json=[]), max_per_hour=2)
        agent.finished_at = [_t.monotonic() - 4000, _t.monotonic() - 3700]

        assert agent.wait_for_slot(sleep=lambda s: None) is True

    def test_hitting_the_cap_mid_batch_leaves_the_rest_queued(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json=[
                        {"id": i, "url": f"https://youtu.be/{i}", "kind": "youtube"}
                        for i in (1, 2, 3)
                    ],
                )
            return httpx.Response(200, json={})

        agent = _agent(handler, max_per_hour=1)
        seen: list[int] = []
        monkeypatch.setattr(agent, "handle", lambda s: seen.append(s["id"]))
        # handle() is stubbed, so record the completion the real one would.
        original = agent.handle

        def handle_and_record(source):
            original(source)
            agent.finished_at.append(__import__("time").monotonic())

        monkeypatch.setattr(agent, "handle", handle_and_record)

        agent.tick()

        assert seen == [1]
