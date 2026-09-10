from helper.audio_capture import LoopbackSource, MicSource


class FakeStream:
    def __init__(self):
        self.read_calls = []
        self.closed = False

    def read(self, frames, exception_on_overflow=False):
        self.read_calls.append(frames)
        return b"\x00" * frames * 2

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class FakePyAudio:
    paInt16 = "paInt16"

    def __init__(self):
        self.opened_with = None
        self.terminated = False
        self.stream = FakeStream()

    def PyAudio(self):
        return self

    def open(self, **kwargs):
        self.opened_with = kwargs
        return self.stream

    def terminate(self):
        self.terminated = True

    def get_default_wasapi_loopback(self):
        return {"index": 7, "defaultSampleRate": 48000.0, "maxInputChannels": 2}

    def get_default_input_device_info(self):
        return {"index": 2, "defaultSampleRate": 48000.0, "maxInputChannels": 1}

    def get_device_info_by_index(self, index):
        return {
            3: {"index": 3, "defaultSampleRate": 44100.0, "maxInputChannels": 1},
        }[index]


def test_mic_source_opens_input_stream_with_requested_params():
    fake_module = FakePyAudio()
    source = MicSource(device_index=3, rate=16000, channels=1, pyaudio_module=fake_module)

    assert fake_module.opened_with["input"] is True
    assert fake_module.opened_with["input_device_index"] == 3
    assert fake_module.opened_with["rate"] == 16000
    assert source.rate == 16000
    assert source.channels == 1


def test_mic_source_read_chunk_delegates_to_stream():
    fake_module = FakePyAudio()
    source = MicSource(pyaudio_module=fake_module)

    chunk = source.read_chunk(480)

    assert fake_module.stream.read_calls == [480]
    assert chunk == b"\x00" * 960


def test_mic_source_close_stops_and_terminates():
    fake_module = FakePyAudio()
    source = MicSource(pyaudio_module=fake_module)

    source.close()

    assert fake_module.stream.closed is True
    assert fake_module.terminated is True


def test_mic_source_queries_default_device_rate_when_not_given():
    fake_module = FakePyAudio()
    source = MicSource(pyaudio_module=fake_module)

    assert fake_module.opened_with["rate"] == 48000
    assert source.rate == 48000


def test_mic_source_queries_specific_device_rate_when_device_index_given():
    fake_module = FakePyAudio()
    source = MicSource(device_index=3, pyaudio_module=fake_module)

    assert fake_module.opened_with["rate"] == 44100
    assert source.rate == 44100


def test_loopback_source_uses_default_wasapi_loopback_device():
    fake_module = FakePyAudio()
    source = LoopbackSource(pyaudio_module=fake_module)

    assert fake_module.opened_with["input_device_index"] == 7
    assert source.rate == 48000
    assert source.channels == 2


from helper.audio_capture import list_input_devices


class FakeMultiDevicePyAudio:
    def __init__(self):
        self._devices = [
            {
                "index": 0,
                "name": "Speakers (Realtek) [Loopback]",
                "maxInputChannels": 2,
                "isLoopbackDevice": True,
            },
            {"index": 1, "name": "Built-in Mic", "maxInputChannels": 1},
            {"index": 2, "name": "USB Headset Mic", "maxInputChannels": 2},
        ]
        self.terminated = False

    def PyAudio(self):
        return self

    def get_device_count(self):
        return len(self._devices)

    def get_device_info_by_index(self, i):
        return self._devices[i]

    def terminate(self):
        self.terminated = True


def test_list_input_devices_filters_to_input_capable_devices():
    fake_module = FakeMultiDevicePyAudio()

    devices = list_input_devices(pyaudio_module=fake_module)

    assert devices == [
        {"index": 1, "name": "Built-in Mic"},
        {"index": 2, "name": "USB Headset Mic"},
    ]


def test_list_input_devices_terminates_pyaudio():
    fake_module = FakeMultiDevicePyAudio()

    list_input_devices(pyaudio_module=fake_module)

    assert fake_module.terminated is True
