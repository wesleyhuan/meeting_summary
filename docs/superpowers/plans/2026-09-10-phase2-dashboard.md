# Phase 2: Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the helper service a browser dashboard (Live / Meetings / Settings tabs) so meeting start/stop, transcript review, and device/model configuration no longer require the raw `/docs` Swagger UI.

**Architecture:** Add a `settings` key-value table and REST endpoints to the existing FastAPI helper; extend `WhisperTranscriber` and `audio_capture` to support the settings that matter (mic device, STT language, Whisper model size); serve a new single-file dashboard (`helper/dashboard.html`, distinct from the legacy root `index.html` prototype) that consumes those endpoints plus the existing `/live` WebSocket.

**Tech Stack:** Same as Phase 1 (FastAPI, SQLite, vanilla JS/HTML/CSS for the dashboard — no new dependencies).

**Spec:** `docs/superpowers/specs/2026-09-08-meeting-transcription-design.md` (see "UI" and "Data model" sections)

## Scoping rulings (this plan narrows the spec's Phase 2 description)

- **In scope:** mic device selection, STT language, Whisper model size (all three directly affect the capture/transcription pipeline already built in Phase 1). Meetings list + transcript viewer. Live tab wired to the real WebSocket/REST instead of Phase 1's Swagger-only flow.
- **Out of scope, deferred:** loopback device selection (the spec mentions it, but `LoopbackSource` always uses the OS default output's loopback today, and most machines have exactly one relevant output device — not worth the added enumeration/wiring complexity yet), `summaries` table, API keys, summary prompt template (all Phase 3/4, per the spec's own phase boundaries).
- **Transcriber caching dropped:** Phase 1's `AppState.transcriber` was a singleton built once and reused. Since Settings now makes `whisper_model_size`/`stt_language` user-editable, `start_meeting` now builds a fresh `WhisperTranscriber` from current settings on every meeting start instead of caching one. Cost: a few hundred ms to reload the model each time you start a meeting; benefit: settings changes always take effect immediately, no stale-cache bugs.

## Global Constraints

- All constraints from Phase 1's plan still apply (Windows-only, `speaker` is exactly `"you"`/`"other"`, one meeting at a time, pause-based captioning, `logging` module at error-prone points with real exception content and context — never bare `print`).
- The dashboard is plain vanilla HTML/CSS/JS — no build step, no framework, consistent with the project's existing `index.html` prototype.
- `helper/dashboard.html` is a NEW file. Do not modify the repo-root `index.html` — it remains the standalone legacy prototype documented in CLAUDE.md's "Running" section, unrelated to the helper service.

---

## File Structure

```
helper/
  db.py              -- MODIFY: add settings table + get_setting/set_setting/get_all_settings
  stt.py             -- MODIFY: WhisperTranscriber gains a `language` constructor param
  audio_capture.py   -- MODIFY: add list_input_devices()
  main.py            -- MODIFY: new REST endpoints, settings-driven meeting start, dashboard route
  dashboard.html     -- NEW: the Phase 2 dashboard (Live/Meetings/Settings tabs)
tests/
  test_db.py             -- MODIFY: settings CRUD tests
  test_stt.py            -- MODIFY: instance-language test
  test_audio_capture.py  -- MODIFY: list_input_devices tests
  test_main.py           -- MODIFY: new endpoint tests + dashboard route test
CLAUDE.md            -- MODIFY: document the dashboard URL
```

---

### Task 1: Settings storage + transcriber/device support

**Files:**
- Modify: `helper/db.py`
- Modify: `helper/stt.py`
- Modify: `helper/audio_capture.py`
- Modify: `tests/test_db.py`
- Modify: `tests/test_stt.py`
- Modify: `tests/test_audio_capture.py`

**Interfaces:**
- Produces: `db.DEFAULT_SETTINGS: dict`, `db.get_setting(conn, key) -> str | None`, `db.set_setting(conn, key, value) -> None`, `db.get_all_settings(conn) -> dict` (used by `main.py` in Task 2). `stt.WhisperTranscriber(model=None, model_size="base", device="cpu", language="en")` — `.transcribe(pcm, sample_rate=16000, language=None)` now defaults to the instance's `language` when not overridden per-call (used by `main.py` in Task 2, and by `pipeline.py` unchanged — pipeline never passes `language` explicitly, so it will pick up whatever the transcriber was constructed with). `audio_capture.list_input_devices(pyaudio_module=None) -> list[dict]` (used by `main.py`'s new `GET /audio-devices` endpoint in Task 2).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py` (append to the existing file, keep the existing tests and fixture as-is):

```python
def test_get_setting_returns_none_when_unset(conn):
    assert db.get_setting(conn, "stt_language") is None


def test_set_and_get_setting(conn):
    db.set_setting(conn, "stt_language", "fr")
    assert db.get_setting(conn, "stt_language") == "fr"


def test_set_setting_overwrites_existing(conn):
    db.set_setting(conn, "stt_language", "fr")
    db.set_setting(conn, "stt_language", "es")
    assert db.get_setting(conn, "stt_language") == "es"


def test_get_all_settings_merges_defaults_with_overrides(conn):
    db.set_setting(conn, "stt_language", "fr")
    settings = db.get_all_settings(conn)
    assert settings["stt_language"] == "fr"
    assert settings["whisper_model_size"] == "base"
    assert settings["mic_device_id"] == ""
```

Add to `tests/test_stt.py` (append, keep existing tests/fixtures as-is):

```python
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
```

This requires `FakeModel` (already defined in `tests/test_stt.py` from Phase 1) to record the language it was called with. Modify the existing `FakeModel` class in `tests/test_stt.py` to:

```python
class FakeModel:
    def __init__(self, segments):
        self._segments = segments
        self.received_language = None

    def transcribe(self, audio, language="en"):
        self.received_language = language
        return iter(self._segments), {"language": language}
```

Add to `tests/test_audio_capture.py` (append, keep existing tests/fixtures as-is):

```python
from helper.audio_capture import list_input_devices


class FakeMultiDevicePyAudio:
    def __init__(self):
        self._devices = [
            {"index": 0, "name": "Speakers (loopback)", "maxInputChannels": 0},
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py tests/test_stt.py tests/test_audio_capture.py -v`
Expected: the new tests FAIL (`AttributeError`/`ImportError`/`AssertionError` depending on the test — settings functions and `list_input_devices` don't exist yet, `FakeModel` doesn't yet record `received_language`).

- [ ] **Step 3: Implement the settings table in `helper/db.py`**

Add to the `SCHEMA` string (inside the same triple-quoted string, after the `transcript_segments` table):

```python
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

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "mic_device_id": "",
    "stt_language": "en",
    "whisper_model_size": "base",
}
```

Add these functions anywhere after `connect()` (e.g. right before `create_meeting`):

```python
def get_setting(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    logger.info("Set setting %s=%r", key, value)


def get_all_settings(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = dict(DEFAULT_SETTINGS)
    settings.update({row["key"]: row["value"] for row in rows})
    return settings
```

- [ ] **Step 4: Implement the `language` parameter in `helper/stt.py`**

Change the `WhisperTranscriber` class to:

```python
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
```

Add `from typing import Optional` to the top of `helper/stt.py` (it currently has no `typing` import).

- [ ] **Step 5: Implement `list_input_devices` in `helper/audio_capture.py`**

Add this function anywhere in the file (e.g. at the end):

```python
def list_input_devices(pyaudio_module=None) -> list[dict]:
    if pyaudio_module is None:
        import pyaudiowpatch as pyaudio_module
    pa = pyaudio_module.PyAudio()
    try:
        devices = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                devices.append({"index": info["index"], "name": info["name"]})
        return devices
    finally:
        pa.terminate()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py tests/test_stt.py tests/test_audio_capture.py -v`
Expected: PASS (existing tests still pass, all new tests pass)

- [ ] **Step 7: Commit**

```bash
git add helper/db.py helper/stt.py helper/audio_capture.py tests/test_db.py tests/test_stt.py tests/test_audio_capture.py
git commit -m "feat: add settings storage, transcriber language, and input device listing"
```

---

### Task 2: REST endpoints for meetings list, settings, and audio devices

**Files:**
- Modify: `helper/main.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `db.list_meetings`, `db.get_all_settings`, `db.set_setting` (Task 1's additions and Phase 1's `db.list_meetings`); `audio_capture.list_input_devices` (Task 1); `stt.WhisperTranscriber(model_size=..., language=...)` (Task 1's new constructor signature).
- Produces: `GET /meetings`, `GET /settings`, `PUT /settings`, `GET /audio-devices` endpoints, and a settings-driven `POST /meetings/start`. Used by `helper/dashboard.html` (Task 3).

- [ ] **Step 1: Write the failing tests**

First, update the existing `isolated_state` fixture and `FakeTranscriber` in `tests/test_main.py` — the fixture currently does `monkeypatch.setattr(main, "WhisperTranscriber", lambda: FakeTranscriber())`, but `start_meeting` will now call `WhisperTranscriber(model_size=..., language=...)` with keyword arguments. Change the fixture's monkeypatch line to:

```python
monkeypatch.setattr(main, "WhisperTranscriber", lambda **kwargs: FakeTranscriber())
```

(Everything else in `isolated_state` stays the same.)

Then add these tests to `tests/test_main.py`:

```python
def test_list_meetings_endpoint_returns_meetings():
    db.create_meeting(main.state.conn, "First")
    db.create_meeting(main.state.conn, "Second")

    with TestClient(main.app) as client:
        response = client.get("/meetings")

    assert response.status_code == 200
    titles = [m["title"] for m in response.json()["meetings"]]
    assert titles == ["Second", "First"]


def test_get_settings_returns_defaults_when_unset():
    with TestClient(main.app) as client:
        response = client.get("/settings")

    assert response.status_code == 200
    assert response.json() == {
        "mic_device_id": "",
        "stt_language": "en",
        "whisper_model_size": "base",
    }


def test_put_settings_updates_and_returns_all_settings():
    with TestClient(main.app) as client:
        response = client.put("/settings", json={"stt_language": "fr", "mic_device_id": "2"})

    assert response.status_code == 200
    body = response.json()
    assert body["stt_language"] == "fr"
    assert body["mic_device_id"] == "2"
    assert body["whisper_model_size"] == "base"


def test_get_audio_devices_returns_list(monkeypatch):
    monkeypatch.setattr(
        main, "list_input_devices", lambda: [{"index": 1, "name": "Mic"}]
    )

    with TestClient(main.app) as client:
        response = client.get("/audio-devices")

    assert response.status_code == 200
    assert response.json() == {"devices": [{"index": 1, "name": "Mic"}]}


def test_start_meeting_passes_settings_to_transcriber_and_mic(monkeypatch):
    db.set_setting(main.state.conn, "stt_language", "fr")
    db.set_setting(main.state.conn, "whisper_model_size", "small")
    db.set_setting(main.state.conn, "mic_device_id", "3")

    captured = {}

    def fake_transcriber_factory(**kwargs):
        captured["transcriber_kwargs"] = kwargs
        return FakeTranscriber()

    def fake_mic_source_factory(device_index=None):
        captured["mic_device_index"] = device_index
        return FakeSource()

    monkeypatch.setattr(main, "WhisperTranscriber", fake_transcriber_factory)
    monkeypatch.setattr(main, "MicSource", fake_mic_source_factory)

    with TestClient(main.app) as client:
        client.post("/meetings/start", json={"title": "Standup"})

    assert captured["transcriber_kwargs"]["language"] == "fr"
    assert captured["transcriber_kwargs"]["model_size"] == "small"
    assert captured["mic_device_index"] == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_main.py -v`
Expected: FAIL — the new endpoints don't exist (404s) and `start_meeting` doesn't read settings yet.

- [ ] **Step 3: Implement the endpoints and settings-driven meeting start in `helper/main.py`**

Update the imports at the top of `helper/main.py`:

```python
from .audio_capture import LoopbackSource, MicSource, list_input_devices
```

Remove the `self.transcriber = None` line from `AppState.__init__` (it's no longer used — every meeting start now builds a fresh transcriber from current settings).

Add a Pydantic model near `StartMeetingRequest`:

```python
class SettingsUpdate(BaseModel):
    mic_device_id: Optional[str] = None
    stt_language: Optional[str] = None
    whisper_model_size: Optional[str] = None
```

Replace the body of `start_meeting` (keep the `@app.post("/meetings/start")` decorator and function signature the same) with:

```python
@app.post("/meetings/start")
def start_meeting(req: StartMeetingRequest):
    if state.active_pipeline is not None:
        raise HTTPException(status_code=409, detail="A meeting is already in progress")

    import datetime

    title = req.title or f"Meeting {datetime.datetime.now().isoformat(timespec='seconds')}"

    settings = db.get_all_settings(state.conn)
    mic_device_id = int(settings["mic_device_id"]) if settings["mic_device_id"] else None
    transcriber = WhisperTranscriber(
        model_size=settings["whisper_model_size"], language=settings["stt_language"]
    )

    mic_source = None
    try:
        mic_source = MicSource(device_index=mic_device_id)
        loopback_source = LoopbackSource()
    except Exception:
        logger.exception("Failed to open audio devices; no meeting row created")
        if mic_source is not None:
            mic_source.close()
        raise HTTPException(status_code=500, detail="Could not open audio devices")

    meeting_id = db.create_meeting(state.conn, title)

    pipeline = build_pipeline(
        meeting_id, state.conn, state.broadcast, mic_source, loopback_source, transcriber
    )
    pipeline.start()
    state.active_pipeline = pipeline
    state.active_meeting_id = meeting_id
    logger.info("Meeting started meeting_id=%s title=%r", meeting_id, title)
    return {"meeting_id": meeting_id, "title": title}
```

Add these new endpoints (anywhere after `start_meeting`, e.g. before `stop_meeting` or after `get_transcript` — placement doesn't matter):

```python
@app.get("/meetings")
def list_meetings():
    rows = db.list_meetings(state.conn)
    return {"meetings": [dict(r) for r in rows]}


@app.get("/settings")
def get_settings():
    return db.get_all_settings(state.conn)


@app.put("/settings")
def update_settings(update: SettingsUpdate):
    data = update.model_dump(exclude_none=True)
    for key, value in data.items():
        db.set_setting(state.conn, key, value)
    return db.get_all_settings(state.conn)


@app.get("/audio-devices")
def audio_devices():
    try:
        devices = list_input_devices()
    except Exception:
        logger.exception("Failed to enumerate audio input devices")
        raise HTTPException(status_code=500, detail="Could not list audio devices")
    return {"devices": devices}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_main.py -v`
Expected: PASS (all existing Phase 1 tests plus the new ones)

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add helper/main.py tests/test_main.py
git commit -m "feat: add meetings list, settings, and audio-device REST endpoints"
```

---

### Task 3: Dashboard UI

**Files:**
- Create: `helper/dashboard.html`
- Modify: `helper/main.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `GET /meetings`, `GET /meetings/{id}/transcript`, `GET /settings`, `PUT /settings`, `GET /audio-devices`, `POST /meetings/start`, `POST /meetings/stop`, `WS /live` — all from Task 2 and Phase 1.
- Produces: `GET /` serving the dashboard HTML.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_main.py`:

```python
def test_dashboard_route_returns_html_with_all_tabs():
    with TestClient(main.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert 'data-tab="live"' in body
    assert 'data-tab="meetings"' in body
    assert 'data-tab="settings"' in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_main.py::test_dashboard_route_returns_html_with_all_tabs -v`
Expected: FAIL (404, route doesn't exist)

- [ ] **Step 3: Add the dashboard route to `helper/main.py`**

Add these imports at the top:

```python
from pathlib import Path

from fastapi.responses import HTMLResponse
```

Add this constant near the top-level constants (e.g. right after `logger = logging.getLogger(__name__)`):

```python
DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"
```

Add this route (place it before the `@app.websocket("/live")` route, anywhere after `app = FastAPI(...)`):

```python
@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_PATH.read_text(encoding="utf-8")
```

- [ ] **Step 4: Create `helper/dashboard.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Live Subtitle Dashboard</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg: #0d0d0f;
      --surface: #17171b;
      --border: #2a2a32;
      --accent: #6c63ff;
      --text: #e8e8f0;
      --text-dim: #7a7a90;
      --interim: #a8a4ff;
      --danger: #ff5f5f;
      --success: #5fff9b;
      --radius: 10px;
    }

    body {
      font-family: 'Segoe UI', system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 14px 24px;
      border-bottom: 1px solid var(--border);
      background: var(--surface);
    }

    header .logo { font-weight: 700; font-size: 1.1rem; }

    nav.tabs {
      display: flex;
      gap: 4px;
      padding: 10px 24px 0;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
    }

    .tab-btn {
      background: none;
      border: none;
      color: var(--text-dim);
      padding: 10px 16px;
      cursor: pointer;
      font-size: 0.9rem;
      border-bottom: 2px solid transparent;
    }

    .tab-btn.active { color: var(--text); border-bottom-color: var(--accent); }

    .tab-panel { display: none; padding: 24px; max-width: 900px; margin: 0 auto; }
    .tab-panel.active { display: block; }

    .btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 8px 18px;
      border: none;
      border-radius: 8px;
      font-size: 0.85rem;
      font-weight: 600;
      cursor: pointer;
      color: #fff;
    }
    .btn-start { background: var(--accent); }
    .btn-stop { background: var(--danger); }
    .btn:disabled { opacity: 0.4; cursor: not-allowed; }

    .status-badge {
      margin-left: 12px;
      padding: 4px 10px;
      border-radius: 20px;
      font-size: 0.78rem;
      background: var(--border);
      color: var(--text-dim);
    }
    .status-badge.active { background: rgba(108,99,255,0.2); color: var(--interim); }

    .subtitle-strip {
      margin-top: 20px;
      padding: 20px;
      background: var(--surface);
      border-radius: var(--radius);
      min-height: 60px;
      font-size: 1.3rem;
      font-weight: 600;
      color: var(--interim);
    }

    .transcript-list {
      margin-top: 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 400px;
      overflow-y: auto;
    }

    .entry { display: flex; gap: 8px; font-size: 0.9rem; }
    .entry .speaker { color: var(--accent); font-weight: 600; min-width: 50px; }
    .entry .ts { color: var(--text-dim); font-size: 0.75rem; }

    .meeting-list { display: flex; flex-direction: column; gap: 8px; }
    .meeting-row {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 12px 16px;
      cursor: pointer;
    }
    .meeting-row:hover { border-color: var(--accent); }
    .meeting-row .title { font-weight: 600; }
    .meeting-row .meta { color: var(--text-dim); font-size: 0.78rem; }

    .settings-form { display: flex; flex-direction: column; gap: 16px; max-width: 420px; }
    .field label { display: block; margin-bottom: 6px; font-size: 0.85rem; color: var(--text-dim); }
    .field select, .field input {
      width: 100%;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      padding: 8px 10px;
      font-size: 0.85rem;
    }

    .toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 10px 18px;
      border-radius: 8px;
      font-size: 0.82rem;
      opacity: 0;
      transform: translateY(10px);
      transition: opacity 0.25s, transform 0.25s;
      pointer-events: none;
    }
    .toast.show { opacity: 1; transform: translateY(0); }
  </style>
</head>
<body>

<header>
  <div class="logo">Live Subtitle Dashboard</div>
</header>

<nav class="tabs">
  <button class="tab-btn active" data-tab="live">Live</button>
  <button class="tab-btn" data-tab="meetings">Meetings</button>
  <button class="tab-btn" data-tab="settings">Settings</button>
</nav>

<section id="tab-live" class="tab-panel active" data-tab="live">
  <button class="btn btn-start" id="btnStart">&#9679; Start Meeting</button>
  <button class="btn btn-stop" id="btnStop" disabled>&#9632; Stop Meeting</button>
  <span class="status-badge" id="statusBadge">Idle</span>

  <div class="subtitle-strip" id="subtitleStrip">Waiting to start&hellip;</div>
  <div class="transcript-list" id="liveTranscript"></div>
</section>

<section id="tab-meetings" class="tab-panel" data-tab="meetings">
  <div class="meeting-list" id="meetingList">Loading&hellip;</div>
  <div class="transcript-list" id="meetingTranscript"></div>
</section>

<section id="tab-settings" class="tab-panel" data-tab="settings">
  <form class="settings-form" id="settingsForm">
    <div class="field">
      <label for="micSelect">Microphone</label>
      <select id="micSelect">
        <option value="">Default device</option>
      </select>
    </div>
    <div class="field">
      <label for="languageInput">STT language code (e.g. en, fr, ja)</label>
      <input type="text" id="languageInput" maxlength="8" />
    </div>
    <div class="field">
      <label for="modelSelect">Whisper model size</label>
      <select id="modelSelect">
        <option value="tiny">tiny (fastest, least accurate)</option>
        <option value="base">base</option>
        <option value="small">small</option>
        <option value="medium">medium</option>
        <option value="large-v3">large-v3 (slowest, most accurate)</option>
      </select>
    </div>
    <button type="submit" class="btn btn-start">Save Settings</button>
  </form>
</section>

<div class="toast" id="toast"></div>

<script>
  const tabButtons = document.querySelectorAll('.tab-btn');
  const tabPanels = document.querySelectorAll('.tab-panel');
  tabButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      tabButtons.forEach((b) => b.classList.remove('active'));
      tabPanels.forEach((p) => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
      if (btn.dataset.tab === 'meetings') loadMeetings();
    });
  });

  function showToast(msg) {
    const toast = document.getElementById('toast');
    toast.textContent = msg;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 2500);
  }

  // ---- Live tab ----
  const btnStart = document.getElementById('btnStart');
  const btnStop = document.getElementById('btnStop');
  const statusBadge = document.getElementById('statusBadge');
  const subtitleStrip = document.getElementById('subtitleStrip');
  const liveTranscript = document.getElementById('liveTranscript');

  let ws = null;
  let currentMeetingId = null;

  function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${location.host}/live`);
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      subtitleStrip.textContent = `${data.speaker === 'you' ? 'You' : 'Other'}: ${data.text}`;
      const entry = document.createElement('div');
      entry.className = 'entry';
      entry.innerHTML = `<span class="speaker">${data.speaker === 'you' ? 'You' : 'Other'}</span><span>${data.text}</span>`;
      liveTranscript.appendChild(entry);
      liveTranscript.scrollTop = liveTranscript.scrollHeight;
    };
    ws.onerror = () => showToast('WebSocket error — is the helper running?');
  }
  connectWebSocket();

  btnStart.addEventListener('click', async () => {
    try {
      const response = await fetch('/meetings/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showToast(body.detail || 'Could not start meeting');
        return;
      }
      const data = await response.json();
      currentMeetingId = data.meeting_id;
      liveTranscript.innerHTML = '';
      subtitleStrip.textContent = 'Listening…';
      btnStart.disabled = true;
      btnStop.disabled = false;
      statusBadge.textContent = 'Listening…';
      statusBadge.classList.add('active');
    } catch (err) {
      showToast('Could not reach the helper service');
    }
  });

  btnStop.addEventListener('click', async () => {
    try {
      await fetch('/meetings/stop', { method: 'POST' });
    } finally {
      btnStart.disabled = false;
      btnStop.disabled = true;
      statusBadge.textContent = 'Idle';
      statusBadge.classList.remove('active');
      subtitleStrip.textContent = 'Waiting to start…';
    }
  });

  // ---- Meetings tab ----
  const meetingList = document.getElementById('meetingList');
  const meetingTranscript = document.getElementById('meetingTranscript');

  async function loadMeetings() {
    meetingList.textContent = 'Loading…';
    try {
      const response = await fetch('/meetings');
      const data = await response.json();
      if (data.meetings.length === 0) {
        meetingList.textContent = 'No meetings recorded yet.';
        return;
      }
      meetingList.innerHTML = '';
      data.meetings.forEach((meeting) => {
        const row = document.createElement('div');
        row.className = 'meeting-row';
        row.innerHTML = `<div class="title">${meeting.title}</div><div class="meta">${meeting.started_at}${meeting.ended_at ? ' → ' + meeting.ended_at : ' (in progress)'}</div>`;
        row.addEventListener('click', () => loadTranscript(meeting.id));
        meetingList.appendChild(row);
      });
    } catch (err) {
      meetingList.textContent = 'Could not load meetings.';
    }
  }

  async function loadTranscript(meetingId) {
    meetingTranscript.innerHTML = 'Loading…';
    try {
      const response = await fetch(`/meetings/${meetingId}/transcript`);
      const data = await response.json();
      meetingTranscript.innerHTML = '';
      data.segments.forEach((seg) => {
        const entry = document.createElement('div');
        entry.className = 'entry';
        entry.innerHTML = `<span class="speaker">${seg.speaker === 'you' ? 'You' : 'Other'}</span><span>${seg.text}</span>`;
        meetingTranscript.appendChild(entry);
      });
    } catch (err) {
      meetingTranscript.textContent = 'Could not load transcript.';
    }
  }

  // ---- Settings tab ----
  const micSelect = document.getElementById('micSelect');
  const languageInput = document.getElementById('languageInput');
  const modelSelect = document.getElementById('modelSelect');
  const settingsForm = document.getElementById('settingsForm');

  async function loadSettings() {
    try {
      const [devicesResponse, settingsResponse] = await Promise.all([
        fetch('/audio-devices'),
        fetch('/settings'),
      ]);
      const devicesData = await devicesResponse.json();
      const settings = await settingsResponse.json();

      devicesData.devices.forEach((device) => {
        const option = document.createElement('option');
        option.value = String(device.index);
        option.textContent = device.name;
        micSelect.appendChild(option);
      });

      micSelect.value = settings.mic_device_id || '';
      languageInput.value = settings.stt_language || 'en';
      modelSelect.value = settings.whisper_model_size || 'base';
    } catch (err) {
      showToast('Could not load settings');
    }
  }
  loadSettings();

  settingsForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await fetch('/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mic_device_id: micSelect.value,
          stt_language: languageInput.value || 'en',
          whisper_model_size: modelSelect.value,
        }),
      });
      showToast('Settings saved');
    } catch (err) {
      showToast('Could not save settings');
    }
  });
</script>
</body>
</html>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_main.py -v`
Expected: PASS

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add helper/main.py helper/dashboard.html tests/test_main.py
git commit -m "feat: add browser dashboard with Live, Meetings, and Settings tabs"
```

---

### Task 4: Documentation and manual verification

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: updated docs. No new runtime interfaces.

- [ ] **Step 1: Update the "Phase 1" section of CLAUDE.md to mention the dashboard**

Read the current `CLAUDE.md` first to find the exact text of the "Phase 1: meeting capture helper + overlay" section, then add a new paragraph immediately after the "Overlay" paragraph (before "**Manual end-to-end test**") along these lines (adjust wording to match the file's existing style, but keep the same information):

```markdown
**Dashboard** (Phase 2 — browser UI for meeting start/stop, history, and settings, served by the same helper): open `http://localhost:8000/` while the helper is running. Live tab mirrors the overlay; Meetings tab lists past meetings and their transcripts; Settings tab lets you pick a mic device, STT language, and Whisper model size (`GET/PUT /settings`, `GET /audio-devices`) — changes take effect on the next meeting you start.
```

Also update the "**Manual end-to-end test**" step 2, which currently says "Open `http://localhost:8000/docs` and call `POST /meetings/start`" — change it to mention the dashboard is now the primary way to do this, while keeping `/docs` as a fallback, e.g.:

```markdown
2. Open `http://localhost:8000/` (the dashboard) and click **Start Meeting** — or use `http://localhost:8000/docs` and call `POST /meetings/start` directly.
```

- [ ] **Step 2: Run the full test suite one more time**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: PASS (all tests from Phase 1 + Phase 2)

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document Phase 2 dashboard in CLAUDE.md"
```

- [ ] **Step 4: Manual verification (performed by the controller with curl, since it requires a running helper — not a subagent's job)**

Start the helper (`uvicorn helper.main:app --port 8000`) and verify: `GET /` returns the dashboard HTML; `GET /meetings` lists real meetings; `GET /settings` returns defaults; `PUT /settings` persists a change and `GET /settings` reflects it; `GET /audio-devices` lists at least one real input device; `POST /meetings/start` after changing `whisper_model_size`/`stt_language` actually uses the new values (visible in the app's log output, now that logging is fixed).
