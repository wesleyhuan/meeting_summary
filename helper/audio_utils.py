import logging

import numpy as np

logger = logging.getLogger(__name__)

TARGET_RATE = 16000


def resample_to_16k_mono(pcm: bytes, in_rate: int, in_channels: int) -> bytes:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)

    if in_channels > 1:
        samples = samples.reshape(-1, in_channels).mean(axis=1)

    if in_rate != TARGET_RATE:
        duration = len(samples) / in_rate
        target_len = max(1, round(duration * TARGET_RATE))
        src_idx = np.linspace(0, len(samples) - 1, num=target_len)
        samples = np.interp(src_idx, np.arange(len(samples)), samples)

    return samples.astype(np.int16).tobytes()
