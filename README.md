# Meeting Summary

Record any meeting on your PC — Webex, Google Meet, Teams, anything — get a live subtitle overlay, a full speaker-labeled transcript, and a summary written by your own Claude subscription.

Everything runs locally. Audio never leaves your machine: capture, voice detection, and speech-to-text all happen on your PC, and transcripts are stored in a local SQLite file. Summarization uses **your existing Claude Desktop subscription through MCP** — no API key, no per-meeting cost.

> **Windows only.** Capturing the audio *other people* produce relies on WASAPI loopback, which is Windows-specific. The rest would port, but this does not.

---

## What it does

- **Captures both sides of the call.** Your microphone *and* your system audio output, as two separate streams — so remote participants are transcribed even though the meeting app never exposes their audio to you.
- **Live captions in an always-on-top overlay** that floats over your meeting window, so you don't have to alt-tab to read along.
- **Stores a speaker-labeled transcript** per meeting, marked `you` vs `other`, merged into one timeline.
- **Summarizes through your own AI subscription.** A built-in MCP server lets Claude Desktop read your transcripts and write summaries back — you just ask it to.
- **Browser dashboard** to start/stop meetings, browse past transcripts and summaries, and pick your mic, language, and Whisper model size.

## How it fits together

Three independent processes share one SQLite database. None of them requires the others to be running.

```
┌──────────────────────────┐        ┌─────────────────────────────┐
│  Overlay (tkinter)       │◄──WS───│  Helper service (FastAPI)   │
│  always-on-top captions  │        │                             │
└──────────────────────────┘        │  mic + system-loopback      │
                                     │  capture → VAD → Whisper    │
┌──────────────────────────┐◄──WS───│  → SQLite → WebSocket       │
│  Dashboard (browser)     │◄─REST──│                             │
│  meetings, transcripts,  │        └──────────────┬──────────────┘
│  summaries, settings     │                       │
└──────────────────────────┘             ┌─────────▼──────────┐
                                          │  SQLite            │
                                          │  meetings,         │
                                          │  transcript_segments,│
                                          │  summaries, settings│
                                          └─────────▲──────────┘
┌──────────────────────────┐                        │
│  Claude Desktop          │──── MCP (stdio) ───────┘
│  (your subscription)     │     mcp_server.py
└──────────────────────────┘
```

Captions finalize on a pause (~0.8s of silence) rather than word-by-word — transcription runs on a dedicated queue so it never blocks audio capture.

## Requirements

- Windows
- Python 3.11+
- A working microphone, and speakers/headphones (system audio is captured via loopback from your default output device)
- For summarization: [Claude Desktop](https://claude.ai/download) with an active subscription

## Setup

```bash
git clone https://github.com/wesleyhuan/meeting_summary.git
cd meeting_summary
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The first meeting you record downloads the `faster-whisper` model (one time, needs internet). Everything after that is offline.

> This project depends on `webrtcvad-wheels` rather than `webrtcvad` — it's the same library, repackaged with prebuilt Windows wheels, so you don't need Microsoft C++ Build Tools to install it.

## Running

**Helper service** — does the actual work (capture, transcription, storage):

```bash
.venv\Scripts\python.exe -m uvicorn helper.main:app --port 8000
```

**Dashboard** — open <http://localhost:8000/> while the helper is running. Three tabs:

- *Live* — start/stop a meeting and watch captions arrive
- *Meetings* — past meetings, their transcripts, and any summaries
- *Settings* — microphone, speech-to-text language, Whisper model size, and the prompt used for summaries

**Overlay** — optional always-on-top caption strip, in its own terminal:

```bash
.venv\Scripts\python.exe -m overlay.overlay
```

Drag it anywhere. Click it once, then press `Esc` to close (it's a borderless window, so it needs focus first).

## Summarization via Claude Desktop

Add this to the `mcpServers` object in `%APPDATA%\Claude\claude_desktop_config.json`, replacing the paths with wherever you cloned the repo:

```json
"livesubtitle": {
  "command": "C:\\path\\to\\meeting_summary\\.venv\\Scripts\\python.exe",
  "args": ["C:\\path\\to\\meeting_summary\\mcp_server.py"]
}
```

If that file already has other MCP servers in it, add this as one more key — don't replace the object. Both paths must be absolute, and `command` must point at the project's venv Python, since the server imports from this repo.

Then fully restart Claude Desktop (quit from the system tray, not just closing the window) and ask it something like *"summarize my last meeting"*. It will find the meeting, read the transcript, follow the prompt you set in Settings, and write the summary back — where it shows up in the dashboard's Meetings tab.

The server exposes four tools: `list_meetings`, `get_meeting_transcript`, `get_summary_prompt`, `save_meeting_summary`.

## Tests

```bash
.venv\Scripts\python.exe -m pytest -v
```

87 tests, covering storage, resampling, VAD segmentation, speech-to-text result parsing, capture-device wiring, pipeline orchestration, the REST and WebSocket API, the MCP tools (including a real end-to-end stdio round-trip against a live subprocess), and overlay caption formatting.

Real audio hardware, the WASAPI loopback device, and the Whisper model itself aren't exercised by the automated suite — those are covered by manual verification.

## Status and known limitations

Built in phases; the first three are done and working:

1. ✅ Core capture, transcription, storage, and the live overlay
2. ✅ Browser dashboard
3. ✅ MCP server for subscription-based summarization
4. ⬜ Optional direct API-key summarization, for people without an MCP client

Known limitations:

- One meeting at a time.
- Speaker labels are `you` vs `other` — no per-person diarization among remote participants.
- Recording is started manually; there's no auto-detection of when a call begins.
- Loopback capture always uses your default output device; it isn't selectable yet.
- The overlay needs a click before `Esc` will close it.

## Repository layout

```
helper/          FastAPI service: audio capture, VAD, Whisper, SQLite, WebSocket, dashboard
overlay/         tkinter always-on-top caption window
mcp_server.py    stdio MCP server for Claude Desktop
tests/           pytest suite
docs/            design spec and per-phase implementation plans
index.html       original standalone prototype (browser Web Speech API) — superseded
python_subtitle.py  original standalone prototype (CLI, mic only) — superseded
```

`index.html` and `python_subtitle.py` are the early prototypes this project grew out of. They still run on their own, but they only hear your microphone and are not part of the current system.
