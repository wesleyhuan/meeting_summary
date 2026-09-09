# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Live Subtitle Generator: two independent, standalone implementations of the same idea (real-time speech-to-text captioning). They do not share code or communicate with each other — pick whichever one the task concerns.

1. **`index.html`** — a single-file browser app using the Web Speech API (`SpeechRecognition` / `webkitSpeechRecognition`). No build step, no dependencies, no server.
2. **`python_subtitle.py`** — a CLI script using the `SpeechRecognition` package with Google's free Speech API and a local microphone (`PyAudio`).

There is no build system, package.json, test suite, or linter configured in this repo.

## Running

**Browser version:** open `index.html` directly in Chrome or Edge (Web Speech API is not supported in Firefox/Safari). No server needed.

**Python version:**
```
pip install -r requirements.txt
python python_subtitle.py
```
On Windows, if `PyAudio` fails to install via pip: `pip install pipwin && pipwin install pyaudio`.

## Phase 1: meeting capture helper + overlay

Two new entry points, in addition to the original `index.html`/`python_subtitle.py` prototype:

**Helper service** (does the real work — audio capture, VAD, Whisper STT, SQLite storage, WebSocket push):
```
uvicorn helper.main:app --port 8000
```
On first meeting start, this downloads the `faster-whisper` "base" model (one-time, requires internet). This project uses `webrtcvad-wheels` (a prebuilt-wheel drop-in for `webrtcvad`) specifically to avoid needing Microsoft C++ Build Tools on Windows — if you ever need to switch back to plain `webrtcvad`, you'd need those build tools installed.

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

## Architecture notes

### `index.html`
Everything (styles, markup, logic) lives in this one file. Key state machine:
- `buildRecognition()` constructs and configures a `SpeechRecognition` instance (`continuous = true`, `interimResults = true`) and wires its `onstart`/`onresult`/`onerror`/`onend` handlers.
- `r.onend` auto-restarts recognition if `isListening` is still `true` — this is what makes "continuous" listening survive the browser's periodic forced stops. Any change to stop/start logic needs to keep this restart-loop invariant intact, or listening will silently die after ~60s.
- Interim vs. final results are distinguished via `result.isFinal` inside `onresult`; finals are appended to the transcript log (`addEntry`) and interims only update the live subtitle strip.
- Changing the language mid-session (`langSelect` change handler) tears down and rebuilds the `recognition` object rather than mutating `.lang` on the live instance, since Web Speech API requires a restart for language changes to take effect.

### `python_subtitle.py`
Uses `recognizer.listen_in_background()` so audio capture/recognition happens on a background thread while the main thread just blocks (`while True: pass`) to stay alive for `KeyboardInterrupt`. The `callback()` function is the equivalent of the browser's `onresult`: it calls `recognize_google()` per phrase (blocking network call), prints/logs the result, and swallows `sr.UnknownValueError` (silence) separately from `sr.RequestError` (API/network failure).

Config constants at the top of the file (`LANGUAGE`, `SAVE_LOG`, `ENERGY_THR`, `PAUSE_SEC`) are the intended place to adjust behavior — there is no CLI argument parsing.
