import logging
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

FRAME_MS = 30
SAMPLE_RATE = 16000
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000) * 2  # 16-bit mono = 960 bytes
SILENCE_FRAMES_TO_END = round(0.8 * 1000 / FRAME_MS)  # ~0.8s pause, per spec

Utterance = Tuple[bytes, datetime]


class UtteranceSegmenter:
    """Buffers 30ms PCM frames; flushes a full utterance after a pause follows speech."""

    def __init__(
        self,
        is_speech_fn: Callable[[bytes], bool],
        silence_frames_to_end: int = SILENCE_FRAMES_TO_END,
    ):
        self._is_speech_fn = is_speech_fn
        self._silence_frames_to_end = silence_frames_to_end
        self._buffer = bytearray()
        self._silence_run = 0
        self._has_speech = False
        self._started_at: Optional[datetime] = None

    def push_frame(self, frame: bytes) -> Optional[Utterance]:
        is_speech = self._is_speech_fn(frame)

        if is_speech:
            if not self._has_speech:
                self._started_at = datetime.now(timezone.utc)
            self._buffer.extend(frame)
            self._has_speech = True
            self._silence_run = 0
            return None

        if not self._has_speech:
            return None  # leading silence before any speech; nothing to buffer

        self._buffer.extend(frame)
        self._silence_run += 1
        if self._silence_run >= self._silence_frames_to_end:
            logger.debug(
                "Utterance boundary after %s silence frames, %s bytes buffered",
                self._silence_run, len(self._buffer),
            )
            return self._flush()
        return None

    def flush(self) -> Optional[Utterance]:
        if self._has_speech:
            return self._flush()
        return None

    def _flush(self) -> Utterance:
        result = bytes(self._buffer)
        started_at = self._started_at
        self._buffer = bytearray()
        self._silence_run = 0
        self._has_speech = False
        self._started_at = None
        return result, started_at
