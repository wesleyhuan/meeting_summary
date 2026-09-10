import threading

import pytest
from fastapi.testclient import TestClient

from helper import db, main
from helper.stt import TranscriptionResult


class FakeSource:
    def __init__(self, rate=16000, channels=1):
        self.rate = rate
        self.channels = channels
        self.closed = False

    def read_chunk(self, frames):
        return b"\x00" * frames * 2

    def close(self):
        self.closed = True


class FakeTranscriber:
    def transcribe(self, pcm, sample_rate=16000, language="en"):
        return TranscriptionResult(text="", confidence=0.0)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    main.state.conn = db.connect(str(tmp_path / "test.db"))
    main.state.active_pipeline = None
    main.state.active_meeting_id = None
    monkeypatch.setattr(main, "MicSource", lambda **kwargs: FakeSource())
    monkeypatch.setattr(main, "LoopbackSource", lambda: FakeSource())
    monkeypatch.setattr(main, "WhisperTranscriber", lambda **kwargs: FakeTranscriber())
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


def test_concurrent_start_meeting_requests_only_start_one_pipeline():
    results = []
    barrier = threading.Barrier(2)

    def call_start():
        barrier.wait()
        response = client.post("/meetings/start", json={"title": "Race"})
        results.append(response.status_code)

    with TestClient(main.app) as client:
        threads = [threading.Thread(target=call_start) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert sorted(results) == [200, 409]
    assert len(db.list_meetings(main.state.conn)) == 1


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


def test_failing_loopback_source_creates_no_meeting_row_and_closes_mic(monkeypatch):
    mic_instances = []

    def make_mic(**kwargs):
        source = FakeSource()
        mic_instances.append(source)
        return source

    def make_failing_loopback():
        raise RuntimeError("no loopback device")

    monkeypatch.setattr(main, "MicSource", make_mic)
    monkeypatch.setattr(main, "LoopbackSource", make_failing_loopback)

    with TestClient(main.app) as client:
        response = client.post("/meetings/start", json={"title": "Standup"})

    assert response.status_code == 500
    assert db.list_meetings(main.state.conn) == []
    assert len(mic_instances) == 1
    assert mic_instances[0].closed is True


def test_stop_meeting_pipeline_failure_clears_state_and_returns_500(monkeypatch):
    class FailingPipeline:
        def stop(self):
            raise RuntimeError("boom")

    with TestClient(main.app) as client:
        start_response = client.post("/meetings/start", json={"title": "Standup"})
        assert start_response.status_code == 200

        real_pipeline = main.state.active_pipeline
        main.state.active_pipeline = FailingPipeline()

        stop_response = client.post("/meetings/stop")
        assert stop_response.status_code == 500

        assert main.state.active_pipeline is None
        assert main.state.active_meeting_id is None

        # Service must not be wedged: a new meeting can start right after.
        next_start = client.post("/meetings/start", json={"title": "Next"})
        assert next_start.status_code == 200

        real_pipeline.stop()  # clean up the worker threads the stub left behind


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


def test_list_meetings_endpoint_returns_meetings():
    db.create_meeting(main.state.conn, "First")
    db.create_meeting(main.state.conn, "Second")

    with TestClient(main.app) as client:
        response = client.get("/meetings")

    assert response.status_code == 200
    titles = [m["title"] for m in response.json()["meetings"]]
    assert titles == ["Second", "First"]


def test_get_settings_returns_defaults_when_unset():
    with TestClient(main.app) as client:
        response = client.get("/settings")

    assert response.status_code == 200
    assert response.json() == {
        "mic_device_id": "",
        "stt_language": "en",
        "whisper_model_size": "base",
    }


def test_put_settings_updates_and_returns_all_settings():
    with TestClient(main.app) as client:
        response = client.put("/settings", json={"stt_language": "fr", "mic_device_id": "2"})

    assert response.status_code == 200
    body = response.json()
    assert body["stt_language"] == "fr"
    assert body["mic_device_id"] == "2"
    assert body["whisper_model_size"] == "base"


def test_put_settings_with_invalid_model_size_returns_422_and_does_not_change_setting():
    with TestClient(main.app) as client:
        client.put("/settings", json={"whisper_model_size": "base"})

        response = client.put("/settings", json={"whisper_model_size": "not-a-real-model"})

        assert response.status_code == 422
        assert db.get_all_settings(main.state.conn)["whisper_model_size"] == "base"


def test_get_audio_devices_returns_list(monkeypatch):
    monkeypatch.setattr(
        main, "list_input_devices", lambda: [{"index": 1, "name": "Mic"}]
    )

    with TestClient(main.app) as client:
        response = client.get("/audio-devices")

    assert response.status_code == 200
    assert response.json() == {"devices": [{"index": 1, "name": "Mic"}]}


def test_start_meeting_passes_settings_to_transcriber_and_mic(monkeypatch):
    db.set_setting(main.state.conn, "stt_language", "fr")
    db.set_setting(main.state.conn, "whisper_model_size", "small")
    db.set_setting(main.state.conn, "mic_device_id", "3")

    captured = {}

    def fake_transcriber_factory(**kwargs):
        captured["transcriber_kwargs"] = kwargs
        return FakeTranscriber()

    def fake_mic_source_factory(device_index=None):
        captured["mic_device_index"] = device_index
        return FakeSource()

    monkeypatch.setattr(main, "WhisperTranscriber", fake_transcriber_factory)
    monkeypatch.setattr(main, "MicSource", fake_mic_source_factory)

    with TestClient(main.app) as client:
        client.post("/meetings/start", json={"title": "Standup"})

    assert captured["transcriber_kwargs"]["language"] == "fr"
    assert captured["transcriber_kwargs"]["model_size"] == "small"
    assert captured["mic_device_index"] == 3


def test_dashboard_route_returns_html_with_all_tabs():
    with TestClient(main.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert 'data-tab="live"' in body
    assert 'data-tab="meetings"' in body
    assert 'data-tab="settings"' in body
