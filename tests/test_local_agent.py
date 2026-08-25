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
