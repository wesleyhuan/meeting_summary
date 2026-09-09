import queue
import time
from datetime import datetime, timezone

from helper import db, pipeline
from helper.pipeline import MeetingPipeline, StreamWorker, build_pipeline
from helper.stt import TranscriptionResult


class FakeSegmenter:
    """Signals an utterance exactly once, on the Nth call to push_frame."""

    def __init__(self, flush_on_call: int, utterance: bytes, started_at=None):
        self._flush_on_call = flush_on_call
        self._utterance = utterance
        self._started_at = started_at or datetime.now(timezone.utc)
        self._calls = 0
        self.flushed = False

    def push_frame(self, frame: bytes):
        self._calls += 1
        if self._calls == self._flush_on_call:
            return self._utterance, self._started_at
        return None

    def flush(self):
        self.flushed = True
        return None


class FakeTranscriber:
    def __init__(self, text: str, confidence: float = 0.75):
        self._text = text
        self._confidence = confidence
        self.received = []

    def transcribe(self, pcm: bytes, sample_rate: int = 16000, language: str = "en"):
        self.received.append(pcm)
        return TranscriptionResult(text=self._text, confidence=self._confidence)


class FakeSource:
    def __init__(self, rate=16000, channels=1, chunk=b"\x00" * 960):
        self.rate = rate
        self.channels = channels
        self._chunk = chunk
        self.closed = False

    def read_chunk(self, frames: int) -> bytes:
        return self._chunk

    def close(self) -> None:
        self.closed = True


def test_process_chunk_enqueues_on_segmenter_flush():
    started_at = datetime.now(timezone.utc)
    segmenter = FakeSegmenter(flush_on_call=1, utterance=b"utterance-bytes", started_at=started_at)
    q: queue.Queue = queue.Queue(maxsize=10)

    worker = StreamWorker(
        source=FakeSource(),
        speaker="you",
        segmenter=segmenter,
        transcription_queue=q,
    )

    worker.process_chunk(b"\x00" * 960)

    assert q.get_nowait() == ("you", b"utterance-bytes", started_at)
    assert q.empty()


def test_process_chunk_does_not_enqueue_when_no_utterance_flushed():
    segmenter = FakeSegmenter(flush_on_call=99, utterance=b"never")
    q: queue.Queue = queue.Queue(maxsize=10)

    worker = StreamWorker(
        source=FakeSource(),
        speaker="you",
        segmenter=segmenter,
        transcription_queue=q,
    )

    worker.process_chunk(b"\x00" * 960)

    assert q.empty()


def test_enqueue_drops_and_warns_when_queue_full(caplog):
    segmenter = FakeSegmenter(flush_on_call=1, utterance=b"utterance-bytes")
    q: queue.Queue = queue.Queue(maxsize=1)
    q.put_nowait(("other", b"already-here", datetime.now(timezone.utc)))

    worker = StreamWorker(
        source=FakeSource(),
        speaker="you",
        segmenter=segmenter,
        transcription_queue=q,
    )

    with caplog.at_level("WARNING"):
        worker.process_chunk(b"\x00" * 960)

    assert q.qsize() == 1  # new item was dropped, queue still holds the original
    assert any("queue" in rec.message.lower() for rec in caplog.records)


class ImmediatelyFailingSource(FakeSource):
    """read_chunk always raises, so the capture thread's _run loop exits on its
    very first iteration without ever calling process_chunk -- keeping this test
    deterministic (no race against a tight read/process loop)."""

    def read_chunk(self, frames: int) -> bytes:
        raise RuntimeError("simulated read failure")


def test_stop_flushes_pending_utterance_into_queue():
    segmenter = FakeSegmenter(flush_on_call=99, utterance=b"never")
    started_at = datetime.now(timezone.utc)
    segmenter.flush = lambda: (b"pending-bytes", started_at)
    q: queue.Queue = queue.Queue(maxsize=10)

    worker = StreamWorker(
        source=ImmediatelyFailingSource(),
        speaker="other",
        segmenter=segmenter,
        transcription_queue=q,
    )
    worker.start()
    worker.stop()

    assert q.get_nowait() == ("other", b"pending-bytes", started_at)


def test_consumer_transcribes_writes_db_and_broadcasts(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    meeting_id = db.create_meeting(conn, "Test")
    broadcasts = []
    transcriber = FakeTranscriber(text="hello")

    mp = build_pipeline(
        meeting_id=meeting_id,
        db_conn=conn,
        broadcast=broadcasts.append,
        mic_source=FakeSource(),
        loopback_source=FakeSource(),
        transcriber=transcriber,
    )

    started_at = datetime.now(timezone.utc)
    mp._transcribe_and_emit("you", b"utterance-bytes", started_at)

    rows = db.get_transcript(conn, meeting_id)
    assert [r["text"] for r in rows] == ["hello"]
    assert rows[0]["started_at"] == started_at.isoformat()
    assert broadcasts == [
        {"meeting_id": meeting_id, "speaker": "you", "text": "hello", "confidence": 0.75}
    ]


def test_consumer_skips_empty_transcription(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    meeting_id = db.create_meeting(conn, "Test")
    broadcasts = []

    mp = build_pipeline(
        meeting_id=meeting_id,
        db_conn=conn,
        broadcast=broadcasts.append,
        mic_source=FakeSource(),
        loopback_source=FakeSource(),
        transcriber=FakeTranscriber(text=""),
    )

    mp._transcribe_and_emit("you", b"utterance-bytes", datetime.now(timezone.utc))

    assert db.get_transcript(conn, meeting_id) == []
    assert broadcasts == []


def test_start_stop_runs_cleanly_with_silence(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    meeting_id = db.create_meeting(conn, "Test")
    broadcasts = []

    pipeline_obj = build_pipeline(
        meeting_id=meeting_id,
        db_conn=conn,
        broadcast=broadcasts.append,
        mic_source=FakeSource(),
        loopback_source=FakeSource(),
        transcriber=FakeTranscriber(text=""),  # never produces text
    )

    pipeline_obj.start()
    time.sleep(0.1)
    pipeline_obj.stop()

    assert db.get_transcript(conn, meeting_id) == []
    assert broadcasts == []


def test_build_pipeline_end_to_end_both_speakers(tmp_path, monkeypatch):
    """Real StreamWorkers + real UtteranceSegmenter + a shared consumer thread should
    produce one DB row and one broadcast per speaker once the pipeline is stopped
    (which flushes each worker's in-progress utterance)."""
    # Force every frame to be classified as speech so no pause boundary is ever hit
    # mid-test; each worker's stop() then flushes exactly one utterance.
    monkeypatch.setattr(pipeline, "make_webrtcvad_speech_fn", lambda: (lambda frame: True))

    conn = db.connect(str(tmp_path / "test.db"))
    meeting_id = db.create_meeting(conn, "Test")
    broadcasts = []

    pipeline_obj = build_pipeline(
        meeting_id=meeting_id,
        db_conn=conn,
        broadcast=broadcasts.append,
        mic_source=FakeSource(),
        loopback_source=FakeSource(),
        transcriber=FakeTranscriber(text="hi"),
    )

    before = datetime.now(timezone.utc)
    pipeline_obj.start()
    time.sleep(0.1)
    pipeline_obj.stop()

    rows = db.get_transcript(conn, meeting_id)
    assert sorted(r["speaker"] for r in rows) == ["other", "you"]
    assert all(r["text"] == "hi" for r in rows)
    # started_at must come from the segmenter (utterance-start time), not a DB
    # default stamped after transcription -- so it should be >= our pre-start mark.
    assert all(before <= datetime.fromisoformat(r["started_at"]) for r in rows)

    assert sorted(b["speaker"] for b in broadcasts) == ["other", "you"]
    assert all(b["text"] == "hi" and b["meeting_id"] == meeting_id for b in broadcasts)
