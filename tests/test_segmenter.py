from datetime import datetime

from helper.segmenter import SILENCE_FRAMES_TO_END, UtteranceSegmenter


def make_scripted_speech_fn(pattern: list[bool]):
    it = iter(pattern)

    def is_speech(_frame: bytes) -> bool:
        return next(it)

    return is_speech


def test_no_speech_never_flushes():
    seg = UtteranceSegmenter(make_scripted_speech_fn([False] * 10))
    results = [seg.push_frame(b"\x00\x00") for _ in range(10)]
    assert all(r is None for r in results)
    assert seg.flush() is None


def test_speech_then_enough_silence_flushes_utterance():
    pattern = [True, True] + [False] * SILENCE_FRAMES_TO_END
    seg = UtteranceSegmenter(make_scripted_speech_fn(pattern))
    frames = [f"frame{i}".encode() for i in range(len(pattern))]

    results = [seg.push_frame(f) for f in frames]
    non_none = [r for r in results if r is not None]

    assert len(non_none) == 1
    utterance, started_at = non_none[0]
    assert utterance == b"".join(frames)
    assert isinstance(started_at, datetime)


def test_brief_pause_does_not_split_utterance():
    short_pause = SILENCE_FRAMES_TO_END - 1
    pattern = [True] + [False] * short_pause + [True] + [False] * SILENCE_FRAMES_TO_END
    seg = UtteranceSegmenter(make_scripted_speech_fn(pattern))
    frames = [f"f{i}".encode() for i in range(len(pattern))]

    results = [seg.push_frame(f) for f in frames]
    non_none = [r for r in results if r is not None]

    assert len(non_none) == 1
    utterance, started_at = non_none[0]
    assert utterance == b"".join(frames)
    assert isinstance(started_at, datetime)


def test_segmenter_resets_after_flush_for_next_utterance():
    pattern = (
        [True] + [False] * SILENCE_FRAMES_TO_END
        + [True] + [False] * SILENCE_FRAMES_TO_END
    )
    seg = UtteranceSegmenter(make_scripted_speech_fn(pattern))
    frames = [f"f{i}".encode() for i in range(len(pattern))]

    results = [seg.push_frame(f) for f in frames]
    non_none = [r for r in results if r is not None]

    assert len(non_none) == 2
    first_started_at = non_none[0][1]
    second_started_at = non_none[1][1]
    assert first_started_at <= second_started_at


def test_flush_with_pending_speech_returns_buffer():
    seg = UtteranceSegmenter(make_scripted_speech_fn([True, True]))
    seg.push_frame(b"a")
    seg.push_frame(b"b")
    utterance, started_at = seg.flush()
    assert utterance == b"ab"
    assert isinstance(started_at, datetime)
    assert seg.flush() is None
