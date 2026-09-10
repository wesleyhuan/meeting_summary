import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    text: str
    confidence: float


class WhisperTranscriber:
    def __init__(
        self,
        model=None,
        model_size: str = "base",
        device: str = "cpu",
        language: str = "en",
    ):
        if model is None:
            from faster_whisper import WhisperModel

            logger.info("Loading faster-whisper model_size=%s device=%s", model_size, device)
            model = WhisperModel(model_size, device=device, compute_type="int8")
        self._model = model
        self._language = language

    def transcribe(
        self, pcm: bytes, sample_rate: int = 16000, language: Optional[str] = None
    ) -> TranscriptionResult:
        language = language or self._language
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        try:
            segments, _info = self._model.transcribe(audio, language=language)
        except Exception:
            logger.exception(
                "Whisper transcription failed for a %s-sample utterance", len(audio)
            )
            return TranscriptionResult(text="", confidence=0.0)

        texts = []
        log_probs = []
        for seg in segments:
            stripped = seg.text.strip()
            if stripped:
                texts.append(stripped)
            log_probs.append(seg.avg_logprob)

        text = " ".join(texts)
        confidence = float(np.exp(np.mean(log_probs))) if log_probs else 0.0
        logger.debug(
            "Transcribed %s samples -> %r (confidence=%.3f)", len(audio), text, confidence
        )
        return TranscriptionResult(text=text, confidence=confidence)
