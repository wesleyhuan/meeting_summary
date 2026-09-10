import logging
from typing import Optional

logger = logging.getLogger(__name__)


class MicSource:
    def __init__(
        self,
        device_index=None,
        rate: Optional[int] = None,
        channels: int = 1,
        frames_per_buffer: int = 1024,
        pyaudio_module=None,
    ):
        if pyaudio_module is None:
            import pyaudiowpatch as pyaudio_module
        self._pa = pyaudio_module.PyAudio()
        if rate is None:
            if device_index is not None:
                device_info = self._pa.get_device_info_by_index(device_index)
            else:
                device_info = self._pa.get_default_input_device_info()
            rate = int(device_info["defaultSampleRate"])
            logger.debug(
                "Queried input device rate device_index=%s rate=%s", device_index, rate
            )
        self.rate = rate
        self.channels = channels
        logger.info(
            "Opening mic stream device_index=%s rate=%s channels=%s",
            device_index, rate, channels,
        )
        self._stream = self._pa.open(
            format=pyaudio_module.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=frames_per_buffer,
        )

    def read_chunk(self, frames: int) -> bytes:
        return self._stream.read(frames, exception_on_overflow=False)

    def close(self) -> None:
        logger.info("Closing mic stream")
        self._stream.stop_stream()
        self._stream.close()
        self._pa.terminate()


class LoopbackSource:
    def __init__(
        self,
        frames_per_buffer: int = 1024,
        pyaudio_module=None,
    ):
        if pyaudio_module is None:
            import pyaudiowpatch as pyaudio_module
        self._pa = pyaudio_module.PyAudio()
        default_speakers = self._pa.get_default_wasapi_loopback()
        self.rate = int(default_speakers["defaultSampleRate"])
        self.channels = default_speakers["maxInputChannels"]
        logger.info(
            "Opening loopback stream device_index=%s rate=%s channels=%s",
            default_speakers["index"], self.rate, self.channels,
        )
        self._stream = self._pa.open(
            format=pyaudio_module.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=default_speakers["index"],
            frames_per_buffer=frames_per_buffer,
        )

    def read_chunk(self, frames: int) -> bytes:
        return self._stream.read(frames, exception_on_overflow=False)

    def close(self) -> None:
        logger.info("Closing loopback stream")
        self._stream.stop_stream()
        self._stream.close()
        self._pa.terminate()


def list_input_devices(pyaudio_module=None) -> list[dict]:
    if pyaudio_module is None:
        import pyaudiowpatch as pyaudio_module
    pa = pyaudio_module.PyAudio()
    try:
        devices = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0 and not info.get("isLoopbackDevice", False):
                devices.append({"index": info["index"], "name": info["name"]})
        return devices
    finally:
        pa.terminate()
