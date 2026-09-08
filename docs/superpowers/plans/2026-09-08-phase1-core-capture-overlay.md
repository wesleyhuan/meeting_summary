# Phase 1: Core Capture + Live Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the local helper service (mic + system-loopback capture → VAD segmentation → Whisper STT → SQLite storage → WebSocket push) and a native always-on-top overlay that displays live captions during a Webex/Meet call — no dashboard yet.

**Architecture:** Three layers stacked bottom-up: pure/testable logic (resampling, segmentation, transcription result parsing) → hardware/model adapters (audio capture, Whisper wrapper) → orchestration (per-stream worker threads feeding a shared pipeline) → thin I/O shells (FastAPI service, tkinter overlay) that wire the above together. Each layer is unit-tested via dependency injection (fake sources/models) except the true hardware/GUI edges, which get a documented manual test.

**Tech Stack:** Python 3.11+, FastAPI + uvicorn (helper service), SQLite (`sqlite3` stdlib), PyAudioWPatch (mic + WASAPI loopback capture), webrtcvad (voice activity detection), faster-whisper (local STT), numpy (resampling/audio math), websocket-client (overlay's WS client), tkinter (overlay GUI), pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-meeting-transcription-design.md`

## Global Constraints

- Windows-only — WASAPI loopback capture via PyAudioWPatch is not portable; do not add cross-platform abstraction for other OSes.
- Speaker values are exactly `"you"` or `"other"` (matches the `transcript_segments.speaker` CHECK constraint in the spec's data model).
- Captions finalize per pause (~0.8s of silence), not word-by-word — this is a deliberate trade-off from the spec, not a bug to fix.
- Only one meeting may be active at a time in Phase 1 (no dashboard yet to manage multiple); starting a second meeting while one is active is a 409 error.
- No dashboard/Settings UI in this phase — meeting start/stop happens via the helper's REST API (testable through FastAPI's auto-generated `/docs` page), and device/model choices use sensible defaults (default mic, default speaker's loopback, Whisper `"base"` model, English).
- Per the user's global CLAUDE.md preferences: use Python's `logging` module (never bare `print`) at error-prone points — external I/O results (audio device opens, STT results), key branch/loop boundaries, and the *actual* exception content inside every `try`/`except` (never swallow silently). Log messages must include enough context (module/function, key variable values) to locate the issue.

---

## File Structure

```
helper/
  __init__.py
  db.py              -- SQLite schema + CRUD for meetings/transcript_segments
  audio_utils.py     -- pure PCM resampling helper
  segmenter.py       -- VAD-driven utterance buffering (pure logic, VAD injected)
  vad.py             -- webrtcvad wrapper producing an is_speech_fn
  stt.py             -- faster-whisper wrapper (WhisperTranscriber)
  audio_capture.py   -- MicSource / LoopbackSource (PyAudioWPatch adapters)
  pipeline.py        -- StreamWorker + MeetingPipeline orchestration
  main.py            -- FastAPI app: REST + WebSocket
overlay/
  __init__.py
  formatting.py      -- pure caption-formatting logic
  overlay.py         -- tkinter always-on-top window + WebSocket client
tests/
  test_db.py
  test_audio_utils.py
  test_segmenter.py
  test_vad.py
  test_stt.py
  test_audio_capture.py
  test_pipeline.py
  test_main.py
  test_overlay_formatting.py
pyproject.toml       -- pytest config (makes `helper`/`overlay` importable)
requirements.txt     -- updated with new dependencies
CLAUDE.md            -- updated with Phase 1 run/test instructions
```

---

### Task 1: Project scaffolding + SQLite storage layer

**Files:**
- Create: `pyproject.toml`
- Create: `helper/__init__.py`
- Create: `helper/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `db.connect(db_path: str) -> sqlite3.Connection`, `db.default_db_path() -> str`, `db.create_meeting(conn, title: str) -> int`, `db.end_meeting(conn, meeting_id: int) -> None`, `db.add_segment(conn, meeting_id: int, speaker: str, text: str, confidence: float | None) -> int`, `db.get_meeting(conn, meeting_id: int) -> sqlite3.Row | None`, `db.get_transcript(conn, meeting_id: int) -> list[sqlite3.Row]`, `db.list_meetings(conn) -> list[sqlite3.Row]`.

- [ ] **Step 1: Add pytest config so the new packages are importable**

Create `pyproject.toml`:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
```

- [ ] **Step 2: Write the failing test for the storage layer**

Create `tests/test_db.py`:

```python
import sqlite3

import pytest

from helper import db


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "test.db"))


def test_create_and_get_meeting(conn):
    meeting_id = db.create_meeting(conn, "Standup")
    row = db.get_meeting(conn, meeting_id)
    assert row["title"] == "Standup"
    assert row["ended_at"] is None


def test_end_meeting_sets_ended_at(conn):
    meeting_id = db.create_meeting(conn, "Standup")
    db.end_meeting(conn, meeting_id)
    row = db.get_meeting(conn, meeting_id)
    assert row["ended_at"] is not None


def test_add_segment_and_get_transcript(conn):
    meeting_id = db.create_meeting(conn, "Standup")
    db.add_segment(conn, meeting_id, "you", "hello there", 0.9)
    db.add_segment(conn, meeting_id, "other", "hi back", 0.8)
    rows = db.get_transcript(conn, meeting_id)
    assert [r["speaker"] for r in rows] == ["you", "other"]
    assert [r["text"] for r in rows] == ["hello there", "hi back"]


def test_add_segment_rejects_bad_speaker(conn):
    meeting_id = db.create_meeting(conn, "Standup")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_segment(conn, meeting_id, "bystander", "oops", None)


def test_list_meetings_orders_most_recent_first(conn):
    first = db.create_meeting(conn, "First")
    second = db.create_meeting(conn, "Second")
    rows = db.list_meetings(conn)
    assert [r["id"] for r in rows] == [second, first]


def test_get_meeting_returns_none_for_missing_id(conn):
    assert db.get_meeting(conn, 999) is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helper'`

- [ ] **Step 4: Implement the storage layer**

Create `helper/__init__.py` (empty).

Create `helper/db.py`:

```python
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
    speaker TEXT NOT NULL CHECK (speaker IN ('you', 'other')),
    text TEXT NOT NULL,
    confidence REAL,
    started_at TEXT NOT NULL
);
"""


def default_db_path() -> str:
    appdata = os.getenv("APPDATA", str(Path.home()))
    db_dir = Path(appdata) / "livesubtitle"
    db_dir.mkdir(parents=True, exist_ok=True)
    path = str(db_dir / "livesubtitle.db")
    logger.info("Using default db path: %s", path)
    return path


def connect(db_path: str) -> sqlite3.Connection:
    logger.info("Connecting to db at %s", db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def create_meeting(conn: sqlite3.Connection, title: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO meetings (title, started_at) VALUES (?, ?)", (title, now)
    )
    conn.commit()
    logger.info("Created meeting id=%s title=%r", cur.lastrowid, title)
    return cur.lastrowid


def end_meeting(conn: sqlite3.Connection, meeting_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE meetings SET ended_at = ? WHERE id = ?", (now, meeting_id))
    conn.commit()
    logger.info("Ended meeting id=%s", meeting_id)


def add_segment(
    conn: sqlite3.Connection,
    meeting_id: int,
    speaker: str,
    text: str,
    confidence: Optional[float],
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO transcript_segments (meeting_id, speaker, text, confidence, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (meeting_id, speaker, text, confidence, now),
    )
    conn.commit()
    logger.debug(
        "Added segment meeting_id=%s speaker=%s confidence=%s text=%r",
        meeting_id, speaker, confidence, text,
    )
    return cur.lastrowid


def get_meeting(conn: sqlite3.Connection, meeting_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()


def get_transcript(conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM transcript_segments WHERE meeting_id = ? ORDER BY started_at",
        (meeting_id,),
    ).fetchall()


def list_meetings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM meetings ORDER BY started_at DESC"
    ).fetchall()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml helper/__init__.py helper/db.py tests/test_db.py
git commit -m "feat: add SQLite storage layer for meetings and transcript segments"
```

---

### Task 2: Audio resampling helper

**Files:**
- Create: `helper/audio_utils.py`
- Test: `tests/test_audio_utils.py`

**Interfaces:**
- Consumes: nothing (pure function, no dependency on earlier tasks).
- Produces: `audio_utils.resample_to_16k_mono(pcm: bytes, in_rate: int, in_channels: int) -> bytes` — used by `pipeline.py` (Task 7).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_audio_utils.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_audio_utils.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helper.audio_utils'`

- [ ] **Step 3: Implement the resampler**

Create `helper/audio_utils.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_audio_utils.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add helper/audio_utils.py tests/test_audio_utils.py
git commit -m "feat: add PCM resampling helper for 16kHz mono conversion"
```

---

### Task 3: Utterance segmenter

**Files:**
- Create: `helper/segmenter.py`
- Test: `tests/test_segmenter.py`

**Interfaces:**
- Consumes: nothing (VAD decision function is injected — real one comes from Task 4).
- Produces: `segmenter.FRAME_BYTES` (int, 960 = 30ms of 16-bit mono @16kHz), `segmenter.UtteranceSegmenter(is_speech_fn: Callable[[bytes], bool], silence_frames_to_end: int = segmenter.SILENCE_FRAMES_TO_END)` with `.push_frame(frame: bytes) -> bytes | None` and `.flush() -> bytes | None`. Used by `pipeline.py` (Task 7) and `vad.py` (Task 4, indirectly as the `is_speech_fn` consumer).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_segmenter.py`:

```python
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
    assert non_none[0] == b"".join(frames)


def test_brief_pause_does_not_split_utterance():
    short_pause = SILENCE_FRAMES_TO_END - 1
    pattern = [True] + [False] * short_pause + [True] + [False] * SILENCE_FRAMES_TO_END
    seg = UtteranceSegmenter(make_scripted_speech_fn(pattern))
    frames = [f"f{i}".encode() for i in range(len(pattern))]

    results = [seg.push_frame(f) for f in frames]
    non_none = [r for r in results if r is not None]

    assert len(non_none) == 1
    assert non_none[0] == b"".join(frames)


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


def test_flush_with_pending_speech_returns_buffer():
    seg = UtteranceSegmenter(make_scripted_speech_fn([True, True]))
    seg.push_frame(b"a")
    seg.push_frame(b"b")
    assert seg.flush() == b"ab"
    assert seg.flush() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_segmenter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helper.segmenter'`

- [ ] **Step 3: Implement the segmenter**

Create `helper/segmenter.py`:

```python
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

FRAME_MS = 30
SAMPLE_RATE = 16000
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000) * 2  # 16-bit mono = 960 bytes
SILENCE_FRAMES_TO_END = round(0.8 * 1000 / FRAME_MS)  # ~0.8s pause, per spec


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

    def push_frame(self, frame: bytes) -> Optional[bytes]:
        is_speech = self._is_speech_fn(frame)

        if is_speech:
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

    def flush(self) -> Optional[bytes]:
        if self._has_speech:
            return self._flush()
        return None

    def _flush(self) -> bytes:
        result = bytes(self._buffer)
        self._buffer = bytearray()
        self._silence_run = 0
        self._has_speech = False
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_segmenter.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add helper/segmenter.py tests/test_segmenter.py
git commit -m "feat: add pause-based utterance segmenter"
```

---

### Task 4: WebRTC VAD wrapper

**Files:**
- Create: `helper/vad.py`
- Test: `tests/test_vad.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `vad.make_webrtcvad_speech_fn(sample_rate: int = 16000, aggressiveness: int = 2) -> Callable[[bytes], bool]` — the real `is_speech_fn` plugged into `UtteranceSegmenter` by `pipeline.py` (Task 7).

- [ ] **Step 1: Write the failing test**

Create `tests/test_vad.py`:

```python
from helper.segmenter import FRAME_BYTES
from helper.vad import make_webrtcvad_speech_fn


def test_silence_frame_is_not_speech():
    is_speech = make_webrtcvad_speech_fn()
    silence_frame = b"\x00" * FRAME_BYTES
    assert is_speech(silence_frame) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_vad.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helper.vad'`

- [ ] **Step 3: Implement the wrapper**

Create `helper/vad.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_vad.py -v`
Expected: PASS (1 test)

- [ ] **Step 5: Commit**

```bash
git add helper/vad.py tests/test_vad.py
git commit -m "feat: add webrtcvad-backed speech detection function"
```

---

### Task 5: Whisper transcriber

**Files:**
- Create: `helper/stt.py`
- Test: `tests/test_stt.py`

**Interfaces:**
- Consumes: nothing new (the real `faster_whisper.WhisperModel` is only imported when `model=None`, so tests never need it installed-and-downloaded).
- Produces: `stt.TranscriptionResult(text: str, confidence: float)`, `stt.WhisperTranscriber(model=None, model_size: str = "base", device: str = "cpu").transcribe(pcm: bytes, sample_rate: int = 16000, language: str = "en") -> TranscriptionResult`. Used by `pipeline.py` (Task 7).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stt.py`:

```python
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

    def transcribe(self, audio, language="en"):
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_stt.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helper.stt'`

- [ ] **Step 3: Implement the transcriber**

Create `helper/stt.py`:

```python
import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    text: str
    confidence: float


class WhisperTranscriber:
    def __init__(self, model=None, model_size: str = "base", device: str = "cpu"):
        if model is None:
            from faster_whisper import WhisperModel

            logger.info("Loading faster-whisper model_size=%s device=%s", model_size, device)
            model = WhisperModel(model_size, device=device, compute_type="int8")
        self._model = model

    def transcribe(
        self, pcm: bytes, sample_rate: int = 16000, language: str = "en"
    ) -> TranscriptionResult:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_stt.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add helper/stt.py tests/test_stt.py
git commit -m "feat: add faster-whisper transcriber wrapper"
```

---

### Task 6: Audio capture sources (mic + system loopback)

**Files:**
- Create: `helper/audio_capture.py`
- Test: `tests/test_audio_capture.py`

**Interfaces:**
- Consumes: nothing new (PyAudioWPatch module is injectable for tests).
- Produces: `audio_capture.MicSource(device_index=None, rate=44100, channels=1, frames_per_buffer=1024, pyaudio_module=None)` and `audio_capture.LoopbackSource(frames_per_buffer=1024, pyaudio_module=None)`, both exposing `.rate: int`, `.channels: int`, `.read_chunk(frames: int) -> bytes`, `.close() -> None`. Used by `main.py` (Task 8) to construct real sources, and by `pipeline.py`/`test_pipeline.py` (Task 7) via fakes satisfying the same shape.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_audio_capture.py`:

```python
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


def test_loopback_source_uses_default_wasapi_loopback_device():
    fake_module = FakePyAudio()
    source = LoopbackSource(pyaudio_module=fake_module)

    assert fake_module.opened_with["input_device_index"] == 7
    assert source.rate == 48000
    assert source.channels == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_audio_capture.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helper.audio_capture'`

- [ ] **Step 3: Implement the capture sources**

Create `helper/audio_capture.py`:

```python
import logging

logger = logging.getLogger(__name__)


class MicSource:
    def __init__(
        self,
        device_index=None,
        rate: int = 44100,
        channels: int = 1,
        frames_per_buffer: int = 1024,
        pyaudio_module=None,
    ):
        if pyaudio_module is None:
            import pyaudiowpatch as pyaudio_module
        self._pa = pyaudio_module.PyAudio()
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_audio_capture.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add helper/audio_capture.py tests/test_audio_capture.py
git commit -m "feat: add mic and WASAPI loopback audio capture sources"
```

---

### Task 7: Pipeline orchestration

**Files:**
- Create: `helper/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `segmenter.FRAME_BYTES`, `segmenter.UtteranceSegmenter`, `vad.make_webrtcvad_speech_fn`, `audio_utils.resample_to_16k_mono`, `stt.WhisperTranscriber`/`TranscriptionResult`, `db.add_segment`, and any object shaped like `audio_capture.MicSource`/`LoopbackSource` (`.rate`, `.channels`, `.read_chunk(frames)`, `.close()`).
- Produces: `pipeline.StreamWorker(source, speaker: str, segmenter, transcriber, on_segment: Callable[[str, str, float], None], frames_per_read: int | None = None)` with `.process_chunk(chunk: bytes) -> None`, `.start() -> None`, `.stop() -> None`; `pipeline.MeetingPipeline(meeting_id, db_conn, broadcast, mic_worker, loopback_worker)` with `.start() -> None`, `.stop() -> None`; `pipeline.build_pipeline(meeting_id: int, db_conn, broadcast: Callable[[dict], None], mic_source, loopback_source, transcriber) -> MeetingPipeline`. Used by `main.py` (Task 8).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pipeline.py`:

```python
import time

from helper import db
from helper.pipeline import MeetingPipeline, StreamWorker, build_pipeline
from helper.stt import TranscriptionResult


class FakeSegmenter:
    """Signals an utterance exactly once, on the Nth call to push_frame."""

    def __init__(self, flush_on_call: int, utterance: bytes):
        self._flush_on_call = flush_on_call
        self._utterance = utterance
        self._calls = 0
        self.flushed = False

    def push_frame(self, frame: bytes):
        self._calls += 1
        if self._calls == self._flush_on_call:
            return self._utterance
        return None

    def flush(self):
        self.flushed = True
        return None


class FakeTranscriber:
    def __init__(self, text: str, confidence: float = 0.75):
        self._text = text
        self._confidence = confidence
        self.received = []

    def transcribe(self, pcm: bytes, sample_rate: int = 16000, language: str = "en"):
        self.received.append(pcm)
        return TranscriptionResult(text=self._text, confidence=self._confidence)


class FakeSource:
    def __init__(self, rate=16000, channels=1, chunk=b"\x00" * 960):
        self.rate = rate
        self.channels = channels
        self._chunk = chunk
        self.closed = False

    def read_chunk(self, frames: int) -> bytes:
        return self._chunk

    def close(self) -> None:
        self.closed = True


def test_process_chunk_emits_on_segmenter_flush():
    segmenter = FakeSegmenter(flush_on_call=1, utterance=b"utterance-bytes")
    transcriber = FakeTranscriber(text="hello")
    emitted = []

    worker = StreamWorker(
        source=FakeSource(),
        speaker="you",
        segmenter=segmenter,
        transcriber=transcriber,
        on_segment=lambda speaker, text, confidence: emitted.append((speaker, text, confidence)),
    )

    worker.process_chunk(b"\x00" * 960)

    assert emitted == [("you", "hello", 0.75)]
    assert transcriber.received == [b"utterance-bytes"]


def test_process_chunk_does_not_emit_when_no_utterance_flushed():
    segmenter = FakeSegmenter(flush_on_call=99, utterance=b"never")
    transcriber = FakeTranscriber(text="hello")
    emitted = []

    worker = StreamWorker(
        source=FakeSource(),
        speaker="you",
        segmenter=segmenter,
        transcriber=transcriber,
        on_segment=lambda speaker, text, confidence: emitted.append((speaker, text, confidence)),
    )

    worker.process_chunk(b"\x00" * 960)

    assert emitted == []


def test_process_chunk_skips_emit_for_empty_transcription():
    segmenter = FakeSegmenter(flush_on_call=1, utterance=b"utterance-bytes")
    transcriber = FakeTranscriber(text="")
    emitted = []

    worker = StreamWorker(
        source=FakeSource(),
        speaker="other",
        segmenter=segmenter,
        transcriber=transcriber,
        on_segment=lambda speaker, text, confidence: emitted.append((speaker, text, confidence)),
    )

    worker.process_chunk(b"\x00" * 960)

    assert emitted == []


def test_start_stop_runs_cleanly_with_silence(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    meeting_id = db.create_meeting(conn, "Test")
    broadcasts = []

    pipeline = build_pipeline(
        meeting_id=meeting_id,
        db_conn=conn,
        broadcast=broadcasts.append,
        mic_source=FakeSource(),
        loopback_source=FakeSource(),
        transcriber=FakeTranscriber(text=""),  # never produces text
    )

    pipeline.start()
    time.sleep(0.1)
    pipeline.stop()

    assert db.get_transcript(conn, meeting_id) == []
    assert broadcasts == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helper.pipeline'`

- [ ] **Step 3: Implement the pipeline**

Create `helper/pipeline.py`:

```python
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from . import db
from .audio_utils import resample_to_16k_mono
from .segmenter import FRAME_BYTES, UtteranceSegmenter
from .vad import make_webrtcvad_speech_fn

logger = logging.getLogger(__name__)


class StreamWorker:
    """Runs one audio stream (mic or loopback) through resample -> segment -> STT -> sink."""

    def __init__(
        self,
        source,
        speaker: str,
        segmenter,
        transcriber,
        on_segment: Callable[[str, str, float], None],
        frames_per_read: Optional[int] = None,
    ):
        self._source = source
        self._speaker = speaker
        self._segmenter = segmenter
        self._transcriber = transcriber
        self._on_segment = on_segment
        self._frames_per_read = frames_per_read or max(1, round(0.03 * source.rate))
        self._buffer = bytearray()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        logger.info("Starting stream worker speaker=%s", self._speaker)
        self._thread.start()

    def stop(self) -> None:
        logger.info("Stopping stream worker speaker=%s", self._speaker)
        self._stop.set()
        self._thread.join(timeout=5)
        utterance = self._segmenter.flush()
        if utterance:
            self._emit(utterance)
        self._source.close()

    def process_chunk(self, chunk: bytes) -> None:
        mono16k = resample_to_16k_mono(chunk, self._source.rate, self._source.channels)
        self._buffer.extend(mono16k)
        while len(self._buffer) >= FRAME_BYTES:
            frame = bytes(self._buffer[:FRAME_BYTES])
            del self._buffer[:FRAME_BYTES]
            utterance = self._segmenter.push_frame(frame)
            if utterance:
                self._emit(utterance)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._source.read_chunk(self._frames_per_read)
            except Exception:
                logger.exception("Read failed for speaker=%s; stopping worker", self._speaker)
                return
            self.process_chunk(chunk)

    def _emit(self, utterance: bytes) -> None:
        result = self._transcriber.transcribe(utterance)
        if not result.text:
            logger.debug("Empty transcription for speaker=%s; skipping", self._speaker)
            return
        self._on_segment(self._speaker, result.text, result.confidence)


@dataclass
class MeetingPipeline:
    meeting_id: int
    db_conn: object
    broadcast: Callable[[dict], None]
    mic_worker: StreamWorker
    loopback_worker: StreamWorker

    def start(self) -> None:
        self.mic_worker.start()
        self.loopback_worker.start()

    def stop(self) -> None:
        self.mic_worker.stop()
        self.loopback_worker.stop()


def build_pipeline(
    meeting_id: int,
    db_conn,
    broadcast: Callable[[dict], None],
    mic_source,
    loopback_source,
    transcriber,
) -> MeetingPipeline:
    def make_on_segment(speaker: str):
        def on_segment(spk: str, text: str, confidence: float) -> None:
            db.add_segment(db_conn, meeting_id, spk, text, confidence)
            broadcast(
                {"meeting_id": meeting_id, "speaker": spk, "text": text, "confidence": confidence}
            )

        return on_segment

    mic_worker = StreamWorker(
        source=mic_source,
        speaker="you",
        segmenter=UtteranceSegmenter(make_webrtcvad_speech_fn()),
        transcriber=transcriber,
        on_segment=make_on_segment("you"),
    )
    loopback_worker = StreamWorker(
        source=loopback_source,
        speaker="other",
        segmenter=UtteranceSegmenter(make_webrtcvad_speech_fn()),
        transcriber=transcriber,
        on_segment=make_on_segment("other"),
    )
    return MeetingPipeline(meeting_id, db_conn, broadcast, mic_worker, loopback_worker)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pipeline.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add helper/pipeline.py tests/test_pipeline.py
git commit -m "feat: add pipeline orchestration wiring capture, VAD, STT, storage"
```

---

### Task 8: FastAPI helper service (REST + WebSocket)

**Files:**
- Create: `helper/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `db.connect`, `db.default_db_path`, `db.create_meeting`, `db.end_meeting`, `db.get_meeting`, `db.get_transcript`; `pipeline.build_pipeline`; `stt.WhisperTranscriber`; `audio_capture.MicSource`, `audio_capture.LoopbackSource`.
- Produces: the FastAPI `app` object with `POST /meetings/start`, `POST /meetings/stop`, `GET /meetings/{meeting_id}/transcript`, `WS /live`. This is the top-level entry point Task 10's `uvicorn` command runs.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_main.py`:

```python
import pytest
from fastapi.testclient import TestClient

from helper import db, main
from helper.stt import TranscriptionResult


class FakeSource:
    def __init__(self, rate=16000, channels=1):
        self.rate = rate
        self.channels = channels

    def read_chunk(self, frames):
        return b"\x00" * frames * 2

    def close(self):
        pass


class FakeTranscriber:
    def transcribe(self, pcm, sample_rate=16000, language="en"):
        return TranscriptionResult(text="", confidence=0.0)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    main.state.conn = db.connect(str(tmp_path / "test.db"))
    main.state.active_pipeline = None
    main.state.active_meeting_id = None
    monkeypatch.setattr(main, "MicSource", lambda: FakeSource())
    monkeypatch.setattr(main, "LoopbackSource", lambda: FakeSource())
    monkeypatch.setattr(main, "WhisperTranscriber", lambda: FakeTranscriber())
    yield
    if main.state.active_pipeline is not None:
        main.state.active_pipeline.stop()
        main.state.active_pipeline = None


def test_start_meeting_creates_row_and_returns_id():
    with TestClient(main.app) as client:
        response = client.post("/meetings/start", json={"title": "Standup"})

    assert response.status_code == 200
    meeting_id = response.json()["meeting_id"]
    assert db.get_meeting(main.state.conn, meeting_id)["title"] == "Standup"


def test_start_meeting_while_active_returns_409():
    with TestClient(main.app) as client:
        client.post("/meetings/start", json={"title": "First"})
        response = client.post("/meetings/start", json={"title": "Second"})

    assert response.status_code == 409


def test_stop_meeting_sets_ended_at():
    with TestClient(main.app) as client:
        start_response = client.post("/meetings/start", json={"title": "Standup"})
        meeting_id = start_response.json()["meeting_id"]
        stop_response = client.post("/meetings/stop")

    assert stop_response.status_code == 200
    assert db.get_meeting(main.state.conn, meeting_id)["ended_at"] is not None


def test_stop_meeting_without_active_meeting_returns_409():
    with TestClient(main.app) as client:
        response = client.post("/meetings/stop")

    assert response.status_code == 409


def test_get_transcript_returns_segments():
    meeting_id = db.create_meeting(main.state.conn, "Standup")
    db.add_segment(main.state.conn, meeting_id, "you", "hi", 0.9)

    with TestClient(main.app) as client:
        response = client.get(f"/meetings/{meeting_id}/transcript")

    assert response.status_code == 200
    body = response.json()
    assert body["segments"][0]["text"] == "hi"


def test_get_transcript_for_missing_meeting_returns_404():
    with TestClient(main.app) as client:
        response = client.get("/meetings/999/transcript")

    assert response.status_code == 404


def test_websocket_receives_broadcast_messages():
    with TestClient(main.app) as client:
        with client.websocket_connect("/live") as websocket:
            main.state.broadcast({"speaker": "you", "text": "hello"})
            message = websocket.receive_text()

    assert message == '{"speaker": "you", "text": "hello"}'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_main.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helper.main'`

- [ ] **Step 3: Implement the FastAPI service**

Create `helper/main.py`:

```python
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import db
from .audio_capture import LoopbackSource, MicSource
from .pipeline import build_pipeline
from .stt import WhisperTranscriber

logger = logging.getLogger(__name__)


class AppState:
    def __init__(self, db_path: Optional[str] = None):
        self.conn = db.connect(db_path or db.default_db_path())
        self.active_pipeline = None
        self.active_meeting_id: Optional[int] = None
        self.transcriber = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.queues: list[asyncio.Queue] = []

    def broadcast(self, message: dict) -> None:
        payload = json.dumps(message)
        if self.loop is None:
            logger.warning("Broadcast attempted before event loop was ready; dropping message")
            return
        for queue in list(self.queues):
            self.loop.call_soon_threadsafe(queue.put_nowait, payload)


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.loop = asyncio.get_running_loop()
    logger.info("Helper service started")
    yield
    logger.info("Helper service shutting down")


app = FastAPI(lifespan=lifespan)


class StartMeetingRequest(BaseModel):
    title: Optional[str] = None


@app.post("/meetings/start")
def start_meeting(req: StartMeetingRequest):
    if state.active_pipeline is not None:
        raise HTTPException(status_code=409, detail="A meeting is already in progress")

    import datetime

    title = req.title or f"Meeting {datetime.datetime.now().isoformat(timespec='seconds')}"
    meeting_id = db.create_meeting(state.conn, title)

    if state.transcriber is None:
        state.transcriber = WhisperTranscriber()

    try:
        mic_source = MicSource()
        loopback_source = LoopbackSource()
    except Exception:
        logger.exception("Failed to open audio devices for meeting_id=%s", meeting_id)
        raise HTTPException(status_code=500, detail="Could not open audio devices")

    pipeline = build_pipeline(
        meeting_id, state.conn, state.broadcast, mic_source, loopback_source, state.transcriber
    )
    pipeline.start()
    state.active_pipeline = pipeline
    state.active_meeting_id = meeting_id
    logger.info("Meeting started meeting_id=%s title=%r", meeting_id, title)
    return {"meeting_id": meeting_id, "title": title}


@app.post("/meetings/stop")
def stop_meeting():
    if state.active_pipeline is None:
        raise HTTPException(status_code=409, detail="No meeting is in progress")

    state.active_pipeline.stop()
    db.end_meeting(state.conn, state.active_meeting_id)
    logger.info("Meeting stopped meeting_id=%s", state.active_meeting_id)
    state.active_pipeline = None
    state.active_meeting_id = None
    return {"status": "stopped"}


@app.get("/meetings/{meeting_id}/transcript")
def get_transcript(meeting_id: int):
    meeting = db.get_meeting(state.conn, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    rows = db.get_transcript(state.conn, meeting_id)
    return {"meeting_id": meeting_id, "segments": [dict(r) for r in rows]}


@app.websocket("/live")
async def live_ws(websocket: WebSocket):
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue()
    state.queues.append(queue)
    logger.info("WebSocket client connected; %s total", len(state.queues))
    try:
        while True:
            payload = await queue.get()
            await websocket.send_text(payload)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    finally:
        state.queues.remove(queue)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_main.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add helper/main.py tests/test_main.py
git commit -m "feat: add FastAPI helper service with meeting lifecycle and live WebSocket"
```

---

### Task 9: Overlay caption formatting + tkinter window

**Files:**
- Create: `overlay/__init__.py`
- Create: `overlay/formatting.py`
- Create: `overlay/overlay.py`
- Test: `tests/test_overlay_formatting.py`

**Interfaces:**
- Consumes: the JSON text broadcast by `helper/main.py`'s `/live` WebSocket (shape: `{"meeting_id": int, "speaker": "you"|"other", "text": str, "confidence": float}`).
- Produces: `overlay.formatting.format_caption(payload: str) -> str`; `overlay.overlay.OverlayApp(ws_url: str = "ws://localhost:8000/live").run() -> None` (manual entry point, not unit tested).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_overlay_formatting.py`:

```python
import json

from overlay.formatting import format_caption


def test_format_caption_labels_you():
    payload = json.dumps({"speaker": "you", "text": "hello there"})
    assert format_caption(payload) == "You: hello there"


def test_format_caption_labels_other():
    payload = json.dumps({"speaker": "other", "text": "hi back"})
    assert format_caption(payload) == "Other: hi back"


def test_format_caption_handles_missing_text():
    payload = json.dumps({"speaker": "you"})
    assert format_caption(payload) == "You: "
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_overlay_formatting.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'overlay'`

- [ ] **Step 3: Implement formatting and the overlay window**

Create `overlay/__init__.py` (empty).

Create `overlay/formatting.py`:

```python
import json
import logging

logger = logging.getLogger(__name__)


def format_caption(payload: str) -> str:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.exception("Received malformed WebSocket payload: %r", payload)
        return ""
    speaker_label = "You" if data.get("speaker") == "you" else "Other"
    return f"{speaker_label}: {data.get('text', '')}"
```

Create `overlay/overlay.py`:

```python
import logging
import threading
import time
import tkinter as tk

import websocket

from .formatting import format_caption

logger = logging.getLogger(__name__)


class OverlayApp:
    def __init__(self, ws_url: str = "ws://localhost:8000/live"):
        self.ws_url = ws_url
        self.root = tk.Tk()
        self.root.title("Live Subtitle")
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.geometry("600x80+100+100")
        self.root.configure(bg="#0d0d0f")
        self.label = tk.Label(
            self.root,
            text="Waiting for helper…",
            fg="#e8e8f0",
            bg="#0d0d0f",
            font=("Segoe UI", 16, "bold"),
            wraplength=580,
            justify="left",
        )
        self.label.pack(expand=True, fill="both", padx=12, pady=12)
        self._bind_drag()
        self._ws_thread = threading.Thread(target=self._run_ws_loop, daemon=True)

    def _bind_drag(self) -> None:
        drag_origin = {"x": 0, "y": 0}

        def start(event):
            drag_origin["x"], drag_origin["y"] = event.x, event.y

        def move(event):
            x = self.root.winfo_x() + event.x - drag_origin["x"]
            y = self.root.winfo_y() + event.y - drag_origin["y"]
            self.root.geometry(f"+{x}+{y}")

        self.root.bind("<ButtonPress-1>", start)
        self.root.bind("<B1-Motion>", move)

    def _set_text(self, text: str) -> None:
        self.root.after(0, lambda: self.label.config(text=text))

    def _run_ws_loop(self) -> None:
        def on_message(_ws, message):
            self._set_text(format_caption(message))

        def on_error(_ws, error):
            logger.error("Overlay WebSocket error: %s", error)
            self._set_text("Waiting for helper…")

        def on_close(_ws, *_args):
            logger.info("Overlay WebSocket closed; will retry")
            self._set_text("Waiting for helper…")

        while True:
            app = websocket.WebSocketApp(
                self.ws_url, on_message=on_message, on_error=on_error, on_close=on_close
            )
            app.run_forever()
            time.sleep(1)

    def run(self) -> None:
        self._ws_thread.start()
        self.root.mainloop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    OverlayApp().run()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_overlay_formatting.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add overlay/__init__.py overlay/formatting.py overlay/overlay.py tests/test_overlay_formatting.py
git commit -m "feat: add always-on-top overlay window with WebSocket caption feed"
```

---

### Task 10: Wire up dependencies, entry points, and manual end-to-end verification

**Files:**
- Modify: `requirements.txt`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: everything from Tasks 1–9.
- Produces: a runnable system (`python -m uvicorn helper.main:app` + `python -m overlay.overlay`), documented for manual verification since real audio hardware and a downloaded Whisper model aren't things automated tests should depend on.

- [ ] **Step 1: Update requirements.txt with the new dependencies**

Edit `requirements.txt` to:

```
SpeechRecognition>=3.10.0
PyAudio>=0.2.13
PyAudioWPatch>=0.2.12.5
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
httpx>=0.27.0
numpy>=1.26.0
faster-whisper>=1.0.0
webrtcvad>=2.0.10
websocket-client>=1.8.0
pytest>=8.0.0
```

- [ ] **Step 2: Install dependencies**

Run: `pip install -r requirements.txt`
Expected: all packages install without error (webrtcvad and faster-whisper both ship prebuilt wheels for Windows; if webrtcvad fails to build, it needs Microsoft C++ Build Tools — note this in CLAUDE.md per Step 4).

- [ ] **Step 3: Run the full test suite**

Run: `pytest -v`
Expected: PASS (all tests from Tasks 1–9, ~36 tests total)

- [ ] **Step 4: Document Phase 1 run/test instructions in CLAUDE.md**

Add a new section to `CLAUDE.md` (after the existing "Running" section):

```markdown
## Phase 1: meeting capture helper + overlay

Two new entry points, in addition to the original `index.html`/`python_subtitle.py` prototype:

**Helper service** (does the real work — audio capture, VAD, Whisper STT, SQLite storage, WebSocket push):
```
uvicorn helper.main:app --port 8000
```
On first meeting start, this downloads the `faster-whisper` "base" model (one-time, requires internet). If `webrtcvad` fails to build on install, install the Microsoft C++ Build Tools first.

**Overlay** (always-on-top live caption window, run separately, in its own terminal):
```
python -m overlay.overlay
```

**Manual end-to-end test** (no dashboard yet — use the helper's auto-generated API docs):
1. Start the helper, then the overlay.
2. Open `http://localhost:8000/docs` and call `POST /meetings/start` (empty body `{}` is fine).
3. Speak into your mic, and/or play audio through your speakers — captions should appear in the overlay window within ~1-2 seconds of a pause.
4. Call `POST /meetings/stop`.
5. Call `GET /meetings/{meeting_id}/transcript` to see the full stored transcript with `"you"`/`"other"` speaker labels.

**Tests:** `pytest -v` — covers storage, resampling, segmentation, VAD wiring, STT result parsing, capture-source wiring, pipeline orchestration, the REST/WebSocket API, and overlay caption formatting via fakes/dependency injection. Real audio hardware, the WASAPI loopback device, and the Whisper model itself are exercised only in the manual test above, not in the automated suite.
```

- [ ] **Step 5: Commit**

```bash
git add requirements.txt CLAUDE.md
git commit -m "docs: add Phase 1 dependencies and run/test instructions"
```

- [ ] **Step 6: Perform the manual end-to-end verification from Step 4 and confirm it works before considering Phase 1 done.**
