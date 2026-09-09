import time

from helper import db
from helper.pipeline import MeetingPipeline, StreamWorker, build_pipeline
from helper.stt import TranscriptionResult


class FakeSegmenter:
    """Signals an utterance exactly once, on the Nth call to push_frame."""

    def __init__(self, flush_on_call: int, utterance: bytes):
        self._flush_on_call = flush_on_call
        self._utterance = utterance
        self._calls = 0
        self.flushed = False

    def push_frame(self, frame: bytes):
        self._calls += 1
        if self._calls == self._flush_on_call:
            return self._utterance
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


def test_process_chunk_emits_on_segmenter_flush():
    segmenter = FakeSegmenter(flush_on_call=1, utterance=b"utterance-bytes")
    transcriber = FakeTranscriber(text="hello")
    emitted = []

    worker = StreamWorker(
        source=FakeSource(),
        speaker="you",
        segmenter=segmenter,
        transcriber=transcriber,
        on_segment=lambda speaker, text, confidence: emitted.append((speaker, text, confidence)),
    )

    worker.process_chunk(b"\x00" * 960)

    assert emitted == [("you", "hello", 0.75)]
    assert transcriber.received == [b"utterance-bytes"]


def test_process_chunk_does_not_emit_when_no_utterance_flushed():
    segmenter = FakeSegmenter(flush_on_call=99, utterance=b"never")
    transcriber = FakeTranscriber(text="hello")
    emitted = []

    worker = StreamWorker(
        source=FakeSource(),
        speaker="you",
        segmenter=segmenter,
        transcriber=transcriber,
        on_segment=lambda speaker, text, confidence: emitted.append((speaker, text, confidence)),
    )

    worker.process_chunk(b"\x00" * 960)

    assert emitted == []


def test_process_chunk_skips_emit_for_empty_transcription():
    segmenter = FakeSegmenter(flush_on_call=1, utterance=b"utterance-bytes")
    transcriber = FakeTranscriber(text="")
    emitted = []

    worker = StreamWorker(
        source=FakeSource(),
        speaker="other",
        segmenter=segmenter,
        transcriber=transcriber,
        on_segment=lambda speaker, text, confidence: emitted.append((speaker, text, confidence)),
    )

    worker.process_chunk(b"\x00" * 960)

    assert emitted == []


def test_start_stop_runs_cleanly_with_silence(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    meeting_id = db.create_meeting(conn, "Test")
    broadcasts = []

    pipeline = build_pipeline(
        meeting_id=meeting_id,
        db_conn=conn,
        broadcast=broadcasts.append,
        mic_source=FakeSource(),
        loopback_source=FakeSource(),
        transcriber=FakeTranscriber(text=""),  # never produces text
    )

    pipeline.start()
    time.sleep(0.1)
    pipeline.stop()

    assert db.get_transcript(conn, meeting_id) == []
    assert broadcasts == []
