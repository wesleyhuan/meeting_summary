import logging
from typing import Callable

import webrtcvad

logger = logging.getLogger(__name__)


def make_webrtcvad_speech_fn(
    sample_rate: int = 16000, aggressiveness: int = 2
) -> Callable[[bytes], bool]:
    vad = webrtcvad.Vad(aggressiveness)
    logger.info(
        "Initialized webrtcvad sample_rate=%s aggressiveness=%s", sample_rate, aggressiveness
    )

    def is_speech(frame: bytes) -> bool:
        try:
            return vad.is_speech(frame, sample_rate)
        except Exception:
            logger.exception("webrtcvad failed on a %s-byte frame; treating as silence", len(frame))
            return False

    return is_speech
