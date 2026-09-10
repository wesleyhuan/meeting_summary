import math
from dataclasses import dataclass

import numpy as np

from helper.stt import WhisperTranscriber


@dataclass
class FakeSegment:
    text: str
    avg_logprob: float


class FakeModel:
    def __init__(self, segments):
        self._segments = segments
        self.received_language = None

    def transcribe(self, audio, language="en"):
        self.received_language = language
        return iter(self._segments), {"language": language}


def test_transcribe_joins_segment_text():
    model = FakeModel([FakeSegment(" hello ", -0.1), FakeSegment("world", -0.3)])
    transcriber = WhisperTranscriber(model=model)
    silence = np.zeros(1600, dtype=np.int16).tobytes()

    result = transcriber.transcribe(silence)

    assert result.text == "hello world"


def test_transcribe_confidence_is_exp_mean_logprob():
    model = FakeModel([FakeSegment("hi", -0.2), FakeSegment("there", -0.4)])
    transcriber = WhisperTranscriber(model=model)
    silence = np.zeros(1600, dtype=np.int16).tobytes()

    result = transcriber.transcribe(silence)

    assert math.isclose(result.confidence, math.exp((-0.2 + -0.4) / 2), rel_tol=1e-6)


def test_transcribe_empty_segments_yields_empty_result():
    model = FakeModel([])
    transcriber = WhisperTranscriber(model=model)
    silence = np.zeros(1600, dtype=np.int16).tobytes()

    result = transcriber.transcribe(silence)

    assert result.text == ""
    assert result.confidence == 0.0


def test_transcribe_uses_instance_language_by_default():
    model = FakeModel([FakeSegment("bonjour", -0.1)])
    transcriber = WhisperTranscriber(model=model, language="fr")
    silence = np.zeros(1600, dtype=np.int16).tobytes()

    transcriber.transcribe(silence)

    assert model.received_language == "fr"


def test_transcribe_language_override_takes_precedence():
    model = FakeModel([FakeSegment("hi", -0.1)])
    transcriber = WhisperTranscriber(model=model, language="fr")
    silence = np.zeros(1600, dtype=np.int16).tobytes()

    transcriber.transcribe(silence, language="en")

    assert model.received_language == "en"
