import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from . import db
from .audio_utils import resample_to_16k_mono
from .segmenter import FRAME_BYTES, UtteranceSegmenter
from .vad import make_webrtcvad_speech_fn

logger = logging.getLogger(__name__)


class StreamWorker:
    """Runs one audio stream (mic or loopback) through resample -> segment -> STT -> sink."""

    def __init__(
        self,
        source,
        speaker: str,
        segmenter,
        transcriber,
        on_segment: Callable[[str, str, float], None],
        frames_per_read: Optional[int] = None,
    ):
        self._source = source
        self._speaker = speaker
        self._segmenter = segmenter
        self._transcriber = transcriber
        self._on_segment = on_segment
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
        utterance = self._segmenter.flush()
        if utterance:
            self._emit(utterance)
        self._source.close()

    def process_chunk(self, chunk: bytes) -> None:
        mono16k = resample_to_16k_mono(chunk, self._source.rate, self._source.channels)
        self._buffer.extend(mono16k)
        while len(self._buffer) >= FRAME_BYTES:
            frame = bytes(self._buffer[:FRAME_BYTES])
            del self._buffer[:FRAME_BYTES]
            utterance = self._segmenter.push_frame(frame)
            if utterance:
                self._emit(utterance)

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

    def _emit(self, utterance: bytes) -> None:
        result = self._transcriber.transcribe(utterance)
        if not result.text:
            logger.debug("Empty transcription for speaker=%s; skipping", self._speaker)
            return
        self._on_segment(self._speaker, result.text, result.confidence)


@dataclass
class MeetingPipeline:
    meeting_id: int
    db_conn: object
    broadcast: Callable[[dict], None]
    mic_worker: StreamWorker
    loopback_worker: StreamWorker

    def start(self) -> None:
        self.mic_worker.start()
        self.loopback_worker.start()

    def stop(self) -> None:
        self.mic_worker.stop()
        self.loopback_worker.stop()


def build_pipeline(
    meeting_id: int,
    db_conn,
    broadcast: Callable[[dict], None],
    mic_source,
    loopback_source,
    transcriber,
) -> MeetingPipeline:
    def make_on_segment(speaker: str):
        def on_segment(spk: str, text: str, confidence: float) -> None:
            db.add_segment(db_conn, meeting_id, spk, text, confidence)
            broadcast(
                {"meeting_id": meeting_id, "speaker": spk, "text": text, "confidence": confidence}
            )

        return on_segment

    mic_worker = StreamWorker(
        source=mic_source,
        speaker="you",
        segmenter=UtteranceSegmenter(make_webrtcvad_speech_fn()),
        transcriber=transcriber,
        on_segment=make_on_segment("you"),
    )
    loopback_worker = StreamWorker(
        source=loopback_source,
        speaker="other",
        segmenter=UtteranceSegmenter(make_webrtcvad_speech_fn()),
        transcriber=transcriber,
        on_segment=make_on_segment("other"),
    )
    return MeetingPipeline(meeting_id, db_conn, broadcast, mic_worker, loopback_worker)
