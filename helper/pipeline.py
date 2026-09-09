import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from . import db
from .audio_utils import resample_to_16k_mono
from .segmenter import FRAME_BYTES, UtteranceSegmenter
from .vad import make_webrtcvad_speech_fn

logger = logging.getLogger(__name__)

# Sentinel placed on the transcription queue to signal the consumer thread to exit.
# A dedicated object (not None) so it can never collide with a real queued item.
_SENTINEL = object()


class StreamWorker:
    """Runs one audio stream (mic or loopback) through resample -> segment -> enqueue.

    Transcription is intentionally NOT done here: it happens on a single shared
    consumer thread (see MeetingPipeline) so the two capture threads never block
    on the (slow, single-model) Whisper call while reading audio.
    """

    def __init__(
        self,
        source,
        speaker: str,
        segmenter,
        transcription_queue: "queue.Queue",
        frames_per_read: Optional[int] = None,
    ):
        self._source = source
        self._speaker = speaker
        self._segmenter = segmenter
        self._queue = transcription_queue
        self._frames_per_read = frames_per_read or max(1, round(0.03 * source.rate))
        self._buffer = bytearray()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        logger.info("Starting stream worker speaker=%s", self._speaker)
        self._thread.start()

    def stop(self) -> None:
        logger.info("Stopping stream worker speaker=%s", self._speaker)
        self._stop.set()
        self._thread.join(timeout=5)
        flushed = self._segmenter.flush()
        if flushed is not None:
            self._enqueue(flushed)
        self._source.close()

    def process_chunk(self, chunk: bytes) -> None:
        mono16k = resample_to_16k_mono(chunk, self._source.rate, self._source.channels)
        self._buffer.extend(mono16k)
        while len(self._buffer) >= FRAME_BYTES:
            frame = bytes(self._buffer[:FRAME_BYTES])
            del self._buffer[:FRAME_BYTES]
            flushed = self._segmenter.push_frame(frame)
            if flushed is not None:
                self._enqueue(flushed)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._source.read_chunk(self._frames_per_read)
            except Exception:
                logger.exception("Read failed for speaker=%s; stopping worker", self._speaker)
                return
            try:
                self.process_chunk(chunk)
            except Exception:
                logger.exception(
                    "process_chunk failed for speaker=%s; continuing", self._speaker
                )

    def _enqueue(self, flushed) -> None:
        utterance, started_at = flushed
        try:
            self._queue.put_nowait((self._speaker, utterance, started_at))
        except queue.Full:
            logger.warning(
                "Transcription queue full (maxsize=%s); dropping utterance for speaker=%s",
                self._queue.maxsize, self._speaker,
            )


@dataclass
class MeetingPipeline:
    meeting_id: int
    db_conn: object
    broadcast: Callable[[dict], None]
    transcriber: object
    mic_worker: StreamWorker
    loopback_worker: StreamWorker
    transcription_queue: "queue.Queue" = field(default_factory=lambda: queue.Queue(maxsize=50))
    _consumer_thread: Optional[threading.Thread] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._consumer_thread = threading.Thread(target=self._consume, daemon=True)

    def start(self) -> None:
        self._consumer_thread.start()
        self.mic_worker.start()
        self.loopback_worker.start()

    def stop(self) -> None:
        # Stop capture first; each worker's stop() flushes its pending utterance
        # onto the shared queue before this returns.
        self.mic_worker.stop()
        self.loopback_worker.stop()
        # Now that no more producers can enqueue, tell the consumer to drain and exit.
        self.transcription_queue.put(_SENTINEL)
        self._consumer_thread.join(timeout=10)

    def _consume(self) -> None:
        while True:
            item = self.transcription_queue.get()
            if item is _SENTINEL:
                logger.info("Transcription consumer draining and exiting")
                return
            speaker, utterance, started_at = item
            try:
                self._transcribe_and_emit(speaker, utterance, started_at)
            except Exception:
                logger.exception(
                    "Failed to transcribe/emit utterance for speaker=%s; continuing", speaker
                )

    def _transcribe_and_emit(
        self, speaker: str, utterance: bytes, started_at: Optional[datetime]
    ) -> None:
        result = self.transcriber.transcribe(utterance)
        if not result.text:
            logger.debug("Empty transcription for speaker=%s; skipping", speaker)
            return
        started_at_str = started_at.isoformat() if started_at is not None else None
        db.add_segment(
            self.db_conn,
            self.meeting_id,
            speaker,
            result.text,
            result.confidence,
            started_at=started_at_str,
        )
        self.broadcast(
            {
                "meeting_id": self.meeting_id,
                "speaker": speaker,
                "text": result.text,
                "confidence": result.confidence,
            }
        )


def build_pipeline(
    meeting_id: int,
    db_conn,
    broadcast: Callable[[dict], None],
    mic_source,
    loopback_source,
    transcriber,
) -> MeetingPipeline:
    transcription_queue: "queue.Queue" = queue.Queue(maxsize=50)

    mic_worker = StreamWorker(
        source=mic_source,
        speaker="you",
        segmenter=UtteranceSegmenter(make_webrtcvad_speech_fn()),
        transcription_queue=transcription_queue,
    )
    loopback_worker = StreamWorker(
        source=loopback_source,
        speaker="other",
        segmenter=UtteranceSegmenter(make_webrtcvad_speech_fn()),
        transcription_queue=transcription_queue,
    )
    return MeetingPipeline(
        meeting_id=meeting_id,
        db_conn=db_conn,
        broadcast=broadcast,
        transcriber=transcriber,
        mic_worker=mic_worker,
        loopback_worker=loopback_worker,
        transcription_queue=transcription_queue,
    )
