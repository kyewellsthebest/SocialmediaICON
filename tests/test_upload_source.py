"""Uploading a video file straight into the pipeline.

The download step is the only part of this system that depends on a platform
choosing to allow it. This route removes that dependency, so the tests care
that a file gets the same treatment a downloaded one would.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from api.main import app

    return TestClient(app)


def test_a_non_video_is_refused_before_anything_is_stored(client):
    response = client.post(
        "/api/sources/upload",
        files={"file": ("notes.txt", io.BytesIO(b"not a video"), "text/plain")},
    )

    assert response.status_code == 422
    assert "not a video" in response.json()["detail"]


def test_the_extension_check_is_case_insensitive(client, monkeypatch):
    """A file off a phone is as likely to be .MOV as .mov."""
    import api.routes.sources as sources

    assert ".mov" in sources.UPLOAD_SUFFIXES
    from pathlib import Path

    assert Path("CLIP.MOV").suffix.lower() in sources.UPLOAD_SUFFIXES


def test_a_readable_file_is_probed_stored_and_queued(monkeypatch, tmp_path):
    """The upload path must hand over exactly what a download would have."""
    import worker.tasks.ingest as ingest

    stored: dict = {}
    queued: list = []

    class FakeStorage:
        kind = "local"

        def put_file(self, local, key):
            stored["key"] = key
            stored["bytes"] = len(open(local, "rb").read())
            return key

    monkeypatch.setattr(ingest, "get_storage", lambda: FakeStorage())
    monkeypatch.setattr(
        ingest, "probe", lambda p: type("I", (), {"duration_s": 742.0})(), raising=False
    )
    monkeypatch.setattr("core.ffmpeg_ops.probe", lambda p: type("I", (), {"duration_s": 742.0})())
    monkeypatch.setattr(ingest, "enqueue", lambda *a, **k: queued.append(a))

    captured = {}

    class FakeSession:
        def get(self, model, ident):
            return type(
                "S",
                (),
                {
                    "title": None,
                    "duration_s": None,
                    "storage_key": None,
                    "status": None,
                    "error": "old",
                },
            )()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_scope():
        session = FakeSession()
        captured["session"] = session
        return session

    monkeypatch.setattr(ingest, "session_scope", fake_scope)

    video = tmp_path / "dig.mp4"
    video.write_bytes(b"\x00" * 2048)

    assert ingest.adopt_upload(7, video, title="A big dig") == 7
    assert stored["key"] == "sources/7/dig.mp4"
    assert stored["bytes"] == 2048
    assert queued and queued[0][0] == "transcribe"


def test_adopt_upload_refuses_a_source_that_does_not_exist(monkeypatch, tmp_path):
    import worker.tasks.ingest as ingest

    monkeypatch.setattr("core.ffmpeg_ops.probe", lambda p: type("I", (), {"duration_s": 1.0})())
    monkeypatch.setattr(
        ingest, "get_storage", lambda: type("S", (), {"put_file": lambda self, a, b: b})()
    )

    class EmptySession:
        def get(self, model, ident):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ingest, "session_scope", lambda: EmptySession())

    video = tmp_path / "x.mp4"
    video.write_bytes(b"\x00")

    with pytest.raises(ValueError, match="no source"):
        ingest.adopt_upload(999, video)


def test_an_empty_file_is_refused(client):
    response = client.post(
        "/api/sources/upload",
        files={"file": ("empty.mp4", io.BytesIO(b""), "video/mp4")},
    )

    assert response.status_code == 422
    assert "empty" in response.json()["detail"]


def test_refusing_a_file_needs_no_database(client):
    """Setup order should not decide whether a wrong file gets a clear answer."""
    from core.config import settings

    assert not settings.has_db  # the point of the test
    response = client.post(
        "/api/sources/upload",
        files={"file": ("song.mp3", io.BytesIO(b"id3"), "audio/mpeg")},
    )

    assert response.status_code == 422
