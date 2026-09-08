import numpy as np

from helper.audio_utils import resample_to_16k_mono


def test_mono_16k_passthrough_is_lossless():
    samples = np.array([100, -100, 32000, -32000], dtype=np.int16)
    result = resample_to_16k_mono(samples.tobytes(), in_rate=16000, in_channels=1)
    assert np.frombuffer(result, dtype=np.int16).tolist() == samples.tolist()


def test_stereo_is_averaged_to_mono():
    # Interleaved L,R,L,R: (10,20) -> 15 ; (30,-30) -> 0
    interleaved = np.array([10, 20, 30, -30], dtype=np.int16)
    result = resample_to_16k_mono(interleaved.tobytes(), in_rate=16000, in_channels=2)
    assert np.frombuffer(result, dtype=np.int16).tolist() == [15, 0]


def test_resample_changes_length_proportionally():
    one_second_at_48k = np.zeros(48000, dtype=np.int16)
    result = resample_to_16k_mono(one_second_at_48k.tobytes(), in_rate=48000, in_channels=1)
    result_samples = np.frombuffer(result, dtype=np.int16)
    assert abs(len(result_samples) - 16000) <= 1
