from helper.segmenter import FRAME_BYTES
from helper.vad import make_webrtcvad_speech_fn


def test_silence_frame_is_not_speech():
    is_speech = make_webrtcvad_speech_fn()
    silence_frame = b"\x00" * FRAME_BYTES
    assert is_speech(silence_frame) is False
