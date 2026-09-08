# Meeting Transcription & Summarization — Design

**Date:** 2026-09-08
**Status:** Approved for planning

## Goal

Turn the existing single-file live-subtitle prototype into a tool that:

1. Captures audio from other meeting apps (Webex, Google Meet, etc.) running on the same Windows machine, not just the browser tab's microphone.
2. Shows live captions in an always-on-top overlay while the meeting app is in focus.
3. Persists a full, speaker-labeled transcript per meeting.
4. Produces a meeting summary, either through an MCP server that lets the user's own AI subscription (e.g. Claude Desktop) do the summarizing, or through a fallback direct API key call for users without an MCP client.

## Non-goals

- No auto-detection of when a meeting starts — recording is manually triggered.
- No true word-by-word streaming ASR — captions finalize per pause (~0.8–2s after an utterance ends), matching the existing `python_subtitle.py` behavior.
- No speaker diarization beyond "you" vs. "other" (two known audio streams, not N-speaker separation).
- No mobile/macOS/Linux support — Windows only (WASAPI loopback capture is Windows-specific).

## Architecture

Three independent Python entry points share one local SQLite database file (e.g. `%APPDATA%/livesubtitle/livesubtitle.db`). None depends on another being open.

```
┌─────────────────────────┐        ┌──────────────────────────┐
│  Native Overlay Window  │◄──WS───│                          │
│  (tkinter, always-on-   │        │   Local Helper Service   │
│  top subtitle strip)    │        │   (Python, FastAPI)      │
└─────────────────────────┘        │                          │
                                    │  - Audio capture         │
┌─────────────────────────┐        │    (mic + system loopback)│
│  Browser Dashboard       │◄──WS──┤  - VAD segmentation      │
│  (evolved index.html,    │◄─REST─┤  - Whisper STT           │
│  meeting list/transcript/│       │  - Meeting lifecycle     │
│  summary viewer, settings)│      │  - Fallback API-key      │
└─────────────────────────┘        │    summarization          │
                                    └───────────┬──────────────┘
                                                │
                                       ┌────────▼─────────┐
                                       │  SQLite database  │
                                       │  (meetings,        │
                                       │  transcript_segments,│
                                       │  summaries, settings)│
                                       └────────▲─────────┘
                                                │ reads/writes
                                    ┌───────────┴──────────────┐
                                    │  MCP Server (stdio script)│
                                    │  tools: list_meetings,    │
                                    │  get_meeting_transcript,   │
                                    │  save_meeting_summary,     │
                                    │  get_summary_prompt         │
                                    └───────────────────────────┘
                                                ▲
                                                │ MCP (stdio)
                                       ┌────────┴────────┐
                                       │  Claude Desktop  │
                                       │  (user's own      │
                                       │  subscription)     │
                                       └─────────────────┘
```

### Component responsibilities

- **Local Helper Service** (FastAPI + WebSocket): the only component that touches audio hardware or runs Whisper. Owns meeting lifecycle (start/stop), live caption fan-out, REST API for the dashboard, and the fallback API-key summarization call.
- **Native Overlay** (tkinter): thin WebSocket client. Shows only the latest caption line(s), a Start/Stop control, drag-to-reposition, always-on-top. No meeting history or settings live here.
- **Browser Dashboard** (evolved `index.html`, served as static files by the helper): Live tab (today's UX, driven by the same WebSocket), Meetings tab (history, full transcript, summaries), Settings tab (audio devices, STT language, Whisper model size, API keys, summary prompt template).
- **MCP Server** (separate stdio script, registered in Claude Desktop's MCP config): reads/writes the shared SQLite file directly. No network dependency on the helper process.

## Audio capture & STT pipeline

1. `POST /meetings/start` creates a `meetings` row and starts two capture threads:
   - **System loopback** (`pyaudiowpatch`, WASAPI loopback) — other participants' audio.
   - **Microphone** (`pyaudio`/`sounddevice`) — the user's own voice.
2. Each stream is segmented by a VAD (e.g. `webrtcvad` or `silero-vad`): audio buffers until ~0.8s of silence, then the buffered utterance is finalized. This is the direct analog of `PAUSE_SEC` in `python_subtitle.py`.
3. Finished utterances are handed to a single shared **faster-whisper** instance via a queue (serialized so the two streams don't contend for the same model), returning text + confidence.
4. Each result is tagged `speaker: "you" | "other"`, timestamped, written to `transcript_segments`, and pushed over WebSocket to the overlay and dashboard.
5. `POST /meetings/stop` joins both capture threads, flushes any in-flight utterance through Whisper, and sets `meetings.ended_at`.

Two streams are merged into one timeline purely by `started_at` ordering when rendered — no cross-stream diarization needed since the source stream already tells us who's speaking.

## Data model (SQLite)

```sql
meetings
  id            INTEGER PRIMARY KEY
  title         TEXT            -- default "Meeting <timestamp>", user-renamable
  started_at    TIMESTAMP
  ended_at      TIMESTAMP NULL

transcript_segments
  id            INTEGER PRIMARY KEY
  meeting_id    INTEGER REFERENCES meetings(id)
  speaker       TEXT            -- "you" | "other"
  text          TEXT
  confidence    REAL NULL
  started_at    TIMESTAMP

summaries
  id            INTEGER PRIMARY KEY
  meeting_id    INTEGER REFERENCES meetings(id)
  provider      TEXT            -- "mcp:claude-desktop" | "api:anthropic" | "api:openai" | ...
  content       TEXT
  created_at    TIMESTAMP

settings
  key           TEXT PRIMARY KEY   -- e.g. "summary_prompt_template", "mic_device_id",
                                    -- "loopback_device_id", "stt_language", "whisper_model_size"
  value         TEXT
```

- A meeting can accumulate multiple `summaries` rows (e.g. regenerated later, or from different providers); the dashboard shows the latest by default but keeps history.
- `settings.summary_prompt_template` is seeded with a fixed default (see below) and is user-editable from the dashboard's Settings tab. Both the MCP flow (via `get_summary_prompt`) and the fallback API flow read from this same setting, so the two paths never diverge on prompt wording.

Default summary prompt template: "Summarize this meeting transcript. Include: key decisions made, action items (with owners if mentioned), and open questions or unresolved topics."

## Summarization flows

**Flow A — MCP (primary):**
1. User asks Claude Desktop (already configured with this project's MCP server) to summarize a meeting.
2. Claude Desktop calls `list_meetings` → `get_meeting_transcript(meeting_id)` → `get_summary_prompt()` → generates the summary using the user's own Claude subscription (no API key involved anywhere in this app) → calls `save_meeting_summary(meeting_id, content)`.
3. The dashboard shows the new summary next time it's opened — it doesn't need to be running for this flow to work.

**Flow B — direct API key (fallback):**
1. User configures an API key for a provider (Anthropic, OpenAI, etc.) in Settings.
2. Dashboard's "Summarize now" button on a meeting sends the transcript + `summary_prompt_template` to that provider's API directly from the helper service, and stores the result as a `summaries` row with `provider = "api:<name>"`.

Both flows write to the same table; the dashboard doesn't need to know which path produced a given summary.

## UI

**Overlay**: borderless, semi-transparent, draggable, always-on-top strip near today's `.subtitle-strip` styling. Shows only the latest line(s). Connects to `ws://localhost:<port>/live`; shows "Waiting for helper…" and retries if the helper isn't up yet.

**Dashboard** (evolved `index.html`):
- *Live tab*: today's UX (subtitle strip + scrolling transcript), fed by the same WebSocket.
- *Meetings tab*: list of past meetings → full transcript (you/other labeled) and summary history.
- *Summarize section* per meeting: if no API key is set, shows instructions to ask an MCP client, referencing the meeting's id/title; if a key is set, shows a "Summarize now" button.
- *Settings tab*: mic/loopback device selection, STT language, Whisper model size, API key(s), summary prompt template (default pre-filled, editable).

## Error handling

- **Audio device missing/denied**: surfaced as a failed response from `POST /meetings/start`; no meeting row is created; dashboard shows a toast.
- **STT failure on one utterance**: logged and skipped; the meeting keeps running.
- **Whisper model not yet downloaded**: downloaded once on first run with a progress indicator in the dashboard; recording is blocked until ready.
- **WebSocket drop** (overlay or dashboard closed/reopened): recording is unaffected, since it's driven by the helper's background threads, not the UI connection. On reconnect, the client backfills via REST before resuming live updates.
- **MCP tool errors** (unknown meeting id, empty transcript): return a clear error string so Claude Desktop can relay it to the user instead of failing silently.
- **Fallback API summarization errors** (bad key, rate limit, network failure): surfaced as a dashboard toast; the transcript is untouched so the user can retry.

## Testing strategy

- **Unit tests** (pytest): VAD segmentation boundaries, SQLite storage layer CRUD (meetings/segments/summaries/settings), MCP tool handlers against a test DB, summary prompt builder, REST endpoint contracts via FastAPI's `TestClient`.
- **Manual/integration**: full start → capture (mic + test loopback audio) → stop → view transcript → summarize via both flows; overlay always-on-top behavior; device-selection UI.
- Whisper output quality itself is not something to assert exact wording on in tests — verify "a transcript is produced for known sample audio," not its precise content.

## Build phases

Each phase is independently usable and gets its own implementation plan when reached:

1. **Phase 1 — Core capture + live overlay.** Helper service with mic+loopback capture, VAD, Whisper STT, SQLite storage, WebSocket push, tkinter overlay. No dashboard yet.
2. **Phase 2 — Dashboard.** Evolve `index.html` into Live/Meetings/Settings tabs served by the helper, backed by REST endpoints for history and settings.
3. **Phase 3 — MCP server.** Standalone stdio script, Claude Desktop config instructions, summary prompt template settings.
4. **Phase 4 — Fallback API-key summarization.** Direct-call path in the dashboard for non-MCP users.
