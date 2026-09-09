import pytest
from fastapi.testclient import TestClient

from helper import db, main
from helper.stt import TranscriptionResult


class FakeSource:
    def __init__(self, rate=16000, channels=1):
        self.rate = rate
        self.channels = channels

    def read_chunk(self, frames):
        return b"\x00" * frames * 2

    def close(self):
        pass


class FakeTranscriber:
    def transcribe(self, pcm, sample_rate=16000, language="en"):
        return TranscriptionResult(text="", confidence=0.0)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    main.state.conn = db.connect(str(tmp_path / "test.db"))
    main.state.active_pipeline = None
    main.state.active_meeting_id = None
    monkeypatch.setattr(main, "MicSource", lambda: FakeSource())
    monkeypatch.setattr(main, "LoopbackSource", lambda: FakeSource())
    monkeypatch.setattr(main, "WhisperTranscriber", lambda: FakeTranscriber())
    yield
    if main.state.active_pipeline is not None:
        main.state.active_pipeline.stop()
        main.state.active_pipeline = None


def test_start_meeting_creates_row_and_returns_id():
    with TestClient(main.app) as client:
        response = client.post("/meetings/start", json={"title": "Standup"})

    assert response.status_code == 200
    meeting_id = response.json()["meeting_id"]
    assert db.get_meeting(main.state.conn, meeting_id)["title"] == "Standup"


def test_start_meeting_while_active_returns_409():
    with TestClient(main.app) as client:
        client.post("/meetings/start", json={"title": "First"})
        response = client.post("/meetings/start", json={"title": "Second"})

    assert response.status_code == 409


def test_stop_meeting_sets_ended_at():
    with TestClient(main.app) as client:
        start_response = client.post("/meetings/start", json={"title": "Standup"})
        meeting_id = start_response.json()["meeting_id"]
        stop_response = client.post("/meetings/stop")

    assert stop_response.status_code == 200
    assert db.get_meeting(main.state.conn, meeting_id)["ended_at"] is not None


def test_stop_meeting_without_active_meeting_returns_409():
    with TestClient(main.app) as client:
        response = client.post("/meetings/stop")

    assert response.status_code == 409


def test_get_transcript_returns_segments():
    meeting_id = db.create_meeting(main.state.conn, "Standup")
    db.add_segment(main.state.conn, meeting_id, "you", "hi", 0.9)

    with TestClient(main.app) as client:
        response = client.get(f"/meetings/{meeting_id}/transcript")

    assert response.status_code == 200
    body = response.json()
    assert body["segments"][0]["text"] == "hi"


def test_get_transcript_for_missing_meeting_returns_404():
    with TestClient(main.app) as client:
        response = client.get("/meetings/999/transcript")

    assert response.status_code == 404


def test_websocket_receives_broadcast_messages():
    with TestClient(main.app) as client:
        with client.websocket_connect("/live") as websocket:
            main.state.broadcast({"speaker": "you", "text": "hello"})
            message = websocket.receive_text()

    assert message == '{"speaker": "you", "text": "hello"}'
