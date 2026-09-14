# Phase 3: MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user summarize a meeting using their own Claude subscription (no API key, no per-call cost) by exposing stored meetings and transcripts to Claude Desktop through a standalone stdio MCP server, and showing the resulting summaries in the dashboard.

**Architecture:** A separate Python entry point (`mcp_server.py`) that Claude Desktop launches over stdio. It imports the already-tested `helper.db` storage layer and talks to the same SQLite file the helper service uses — no network coupling, no dependency on the helper process running. Four tools (`list_meetings`, `get_meeting_transcript`, `get_summary_prompt`, `save_meeting_summary`) let Claude Desktop pull a transcript, generate a summary with the user's configured prompt, and write it back. The dashboard gains a read path so those summaries become visible.

**Tech Stack:** Same as Phases 1-2, plus the MCP Python SDK (`mcp` 2.2.0, verified installed and working on this machine).

**Spec:** `docs/superpowers/specs/2026-09-08-meeting-transcription-design.md` (see "Summarization flows" → Flow A, "Data model" → `summaries`/`settings`, and "Error handling" → MCP tool errors)

## Verified environment facts (confirmed by running code on this machine — do not substitute recalled API shapes)

These were established by installing `mcp` and running a real stdio server + client end-to-end. Several contradict pre-2.x training priors:

1. **`FastMCP` does not exist in mcp 2.x.** It was renamed: `from mcp.server.mcpserver import MCPServer`. Importing `mcp.server.fastmcp` raises `ModuleNotFoundError` with a migration message.
2. **Tool errors are masked unless you raise `ToolError`.** From `mcp/server/mcpserver/tools/base.py`: a `ToolError` propagates as `"Error executing tool {name}: {your message}"`, but **any other exception type** (including `ValueError`) is replaced with the generic `"Error executing tool {name}"` and your message never reaches the client. The spec requires clear, relayable error text, so every deliberate tool error in this phase MUST raise `from mcp.server.mcpserver.exceptions import ToolError`. Verified both behaviors on the wire.
3. **`server.run()` defaults to `transport="stdio"`** — signature is `run(self, transport: Literal['stdio','sse','streamable-http'] = 'stdio', **kwargs)`.
4. **`@server.tool()` returns the original function unchanged** (`Callable[[_CallableT], _CallableT]`), so decorated tools can be imported and called directly in unit tests without going through the protocol.
5. **Client-side test API:** `from mcp import ClientSession, StdioServerParameters`, `from mcp.client.stdio import stdio_client`. Result attributes are snake_case: `result.content` (list of blocks with `.type`/`.text`), `result.structured_content`, `result.is_error`. (`structuredContent` camelCase does **not** exist and raises `AttributeError`.)
6. **Claude Desktop is installed on this machine** with config at `%APPDATA%\Claude\claude_desktop_config.json`, containing an `mcpServers` object that **already has 8 entries** (brave-search, github, puppeteer, memory, everything, filesystem, sequential-thinking, notion). Any config instructions must ADD a key, never replace the object.

## Global Constraints

- All Phase 1/2 constraints still apply (Windows-only, `speaker` is exactly `"you"`/`"other"`, one meeting at a time, `logging` module at error-prone points with real exception content and context — never bare `print`).
- **stdout is the JSON-RPC transport.** In `mcp_server.py`, anything written to stdout corrupts the protocol and breaks the connection. Never `print()`. Logging must be pinned to stderr explicitly (`logging.basicConfig(..., stream=sys.stderr)`) — do not rely on the default, and do not import `helper.main` (which calls `basicConfig` with its own settings at import time).
- Summaries written via MCP use `provider = "mcp:claude-desktop"`. The `provider` column is free-form text so Phase 4's API path can write `"api:anthropic"` etc. without a schema change.
- Phase 4 (direct API-key summarization) stays out of scope: no API keys, no outbound LLM calls from this codebase. This phase only reads/writes the database and speaks MCP.

## Scoping rulings (this plan settles what the spec left open)

- **Dashboard display of summaries is IN scope.** The spec lists it under the Phase 2 dashboard description, but summaries didn't exist until now — a summary Claude Desktop writes that the user cannot see anywhere is not a usable increment. Task 3 adds a read endpoint and renders summaries in the Meetings tab.
- **The summary prompt template is editable in Settings** (Task 3), and `get_summary_prompt()` returns that same stored value, so the MCP flow and any future API flow can never diverge on wording.
- **Writing to `claude_desktop_config.json` is NOT automated.** Task 4 produces the exact JSON snippet and instructions; the controller offers to merge it but must ask first, because that file lives outside this repo, affects the user's Claude Desktop setup, and already contains 8 working server entries.

---

## File Structure

```
mcp_server.py            -- NEW: standalone stdio MCP entry point (repo root, next to helper/ and overlay/)
helper/
  db.py                  -- MODIFY: summaries table, summary_prompt_template default, add_summary/get_summaries
  main.py                -- MODIFY: GET /meetings/{id}/summaries endpoint
  dashboard.html         -- MODIFY: render summaries in Meetings tab, prompt template field in Settings
tests/
  test_db.py             -- MODIFY: summaries CRUD tests
  test_mcp_server.py     -- NEW: unit tests for the 4 tools + one stdio integration test
  test_main.py           -- MODIFY: summaries endpoint tests
requirements.txt         -- MODIFY: add mcp>=2.2.0
CLAUDE.md                -- MODIFY: Phase 3 section with Claude Desktop setup
```

`mcp_server.py` lives at the repo root (not inside `helper/`) because it is a third top-level entry point alongside the helper service and the overlay, and because Claude Desktop launches it by absolute file path.

---

### Task 1: Summaries storage + prompt template setting

**Files:**
- Modify: `helper/db.py`
- Modify: `tests/test_db.py`

**Interfaces:**
- Consumes: existing `db.connect`, `db.create_meeting`, `db.DEFAULT_SETTINGS`, `db.get_all_settings` (Phase 1/2, unchanged).
- Produces: `db.add_summary(conn, meeting_id: int, provider: str, content: str) -> int`; `db.get_summaries(conn, meeting_id: int) -> list[sqlite3.Row]` (newest first); `db.DEFAULT_SETTINGS["summary_prompt_template"]` (str). Used by `mcp_server.py` (Task 2) and `helper/main.py` (Task 3).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py` (keep every existing test and the existing `conn` fixture exactly as they are):

```python
def test_add_summary_and_get_summaries(conn):
    meeting_id = db.create_meeting(conn, "Standup")
    db.add_summary(conn, meeting_id, "mcp:claude-desktop", "Key decision: ship it.")
    rows = db.get_summaries(conn, meeting_id)
    assert len(rows) == 1
    assert rows[0]["provider"] == "mcp:claude-desktop"
    assert rows[0]["content"] == "Key decision: ship it."
    assert rows[0]["created_at"] is not None


def test_get_summaries_returns_newest_first(conn):
    meeting_id = db.create_meeting(conn, "Standup")
    first = db.add_summary(conn, meeting_id, "mcp:claude-desktop", "first")
    second = db.add_summary(conn, meeting_id, "mcp:claude-desktop", "second")
    rows = db.get_summaries(conn, meeting_id)
    assert [r["id"] for r in rows] == [second, first]


def test_get_summaries_is_scoped_to_one_meeting(conn):
    meeting_a = db.create_meeting(conn, "A")
    meeting_b = db.create_meeting(conn, "B")
    db.add_summary(conn, meeting_a, "mcp:claude-desktop", "summary for A")
    assert db.get_summaries(conn, meeting_b) == []


def test_get_summaries_empty_for_meeting_without_summaries(conn):
    meeting_id = db.create_meeting(conn, "Standup")
    assert db.get_summaries(conn, meeting_id) == []


def test_summary_prompt_template_has_a_default(conn):
    settings = db.get_all_settings(conn)
    assert "key decisions" in settings["summary_prompt_template"].lower()


def test_summary_prompt_template_is_overridable(conn):
    db.set_setting(conn, "summary_prompt_template", "Just the action items please.")
    assert db.get_all_settings(conn)["summary_prompt_template"] == "Just the action items please."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py -v`
Expected: the six new tests FAIL with `AttributeError: module 'helper.db' has no attribute 'add_summary'` (and `KeyError: 'summary_prompt_template'` for the settings ones).

- [ ] **Step 3: Add the summaries table to the schema**

In `helper/db.py`, add a third table to the `SCHEMA` string, after the existing `settings` table and inside the same triple-quoted string:

```python
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
    provider TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

- [ ] **Step 4: Add the prompt template default**

In `helper/db.py`, add one entry to the existing `DEFAULT_SETTINGS` dict (keep the three existing keys):

```python
DEFAULT_SETTINGS = {
    "mic_device_id": "",
    "stt_language": "en",
    "whisper_model_size": "base",
    "summary_prompt_template": (
        "Summarize this meeting transcript. Include: key decisions made, "
        "action items (with owners if mentioned), and open questions or "
        "unresolved topics."
    ),
}
```

- [ ] **Step 5: Add the summaries CRUD functions**

In `helper/db.py`, add these two functions after `get_all_settings`:

```python
def add_summary(
    conn: sqlite3.Connection, meeting_id: int, provider: str, content: str
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO summaries (meeting_id, provider, content, created_at) "
        "VALUES (?, ?, ?, ?)",
        (meeting_id, provider, content, now),
    )
    conn.commit()
    logger.info(
        "Added summary id=%s meeting_id=%s provider=%s chars=%s",
        cur.lastrowid, meeting_id, provider, len(content),
    )
    return cur.lastrowid


def get_summaries(conn: sqlite3.Connection, meeting_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM summaries WHERE meeting_id = ? ORDER BY created_at DESC, id DESC",
        (meeting_id,),
    ).fetchall()
```

The `, id DESC` tiebreaker is deliberate and matches the fix already applied to `list_meetings`/`get_transcript`: two rows written in the same microsecond would otherwise sort unpredictably.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py -v`
Expected: PASS (all pre-existing tests plus the six new ones).

- [ ] **Step 7: Commit**

```bash
git add helper/db.py tests/test_db.py
git commit -m "feat: add summaries table and summary prompt template setting"
```

---

### Task 2: The MCP server

**Files:**
- Create: `mcp_server.py`
- Create: `tests/test_mcp_server.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `db.connect`, `db.default_db_path`, `db.list_meetings`, `db.get_meeting`, `db.get_transcript`, `db.get_all_settings` (Phase 1/2); `db.add_summary` (Task 1).
- Produces: module `mcp_server` with `server` (an `MCPServer`), `_get_conn()`, and four directly-callable tool functions: `list_meetings() -> list[dict]`, `get_meeting_transcript(meeting_id: int) -> dict`, `get_summary_prompt() -> str`, `save_meeting_summary(meeting_id: int, content: str) -> str`. Honors the `LIVESUBTITLE_DB_PATH` environment variable to override the database location (used by tests and by anyone running against a non-default database).

- [ ] **Step 1: Add the dependency**

Append one line to `requirements.txt` (keep all existing lines):

```
mcp>=2.2.0
```

Then run: `.venv/Scripts/python.exe -m pip install -r requirements.txt`
Expected: completes without error (`mcp` 2.2.0 is already installed in this venv; this confirms the pin resolves).

- [ ] **Step 2: Write the failing tests**

Create `tests/test_mcp_server.py`:

```python
import asyncio
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError

import mcp_server
from helper import db

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Point the module's lazy connection at a throwaway database."""
    connection = db.connect(str(tmp_path / "test.db"))
    monkeypatch.setattr(mcp_server, "_conn", connection)
    return connection


def test_list_meetings_returns_meetings_newest_first(conn):
    first = db.create_meeting(conn, "First")
    second = db.create_meeting(conn, "Second")

    result = mcp_server.list_meetings()

    assert [m["id"] for m in result] == [second, first]
    assert result[0]["title"] == "Second"
    assert "started_at" in result[0]


def test_list_meetings_empty_database_returns_empty_list(conn):
    assert mcp_server.list_meetings() == []


def test_get_meeting_transcript_returns_speaker_labeled_segments(conn):
    meeting_id = db.create_meeting(conn, "Standup")
    db.add_segment(conn, meeting_id, "you", "hello there", 0.9)
    db.add_segment(conn, meeting_id, "other", "hi back", 0.8)

    result = mcp_server.get_meeting_transcript(meeting_id)

    assert result["meeting_id"] == meeting_id
    assert result["title"] == "Standup"
    assert [s["speaker"] for s in result["segments"]] == ["you", "other"]
    assert [s["text"] for s in result["segments"]] == ["hello there", "hi back"]


def test_get_meeting_transcript_unknown_id_raises_toolerror_with_clear_message(conn):
    with pytest.raises(ToolError, match="999"):
        mcp_server.get_meeting_transcript(999)


def test_get_meeting_transcript_empty_transcript_raises_toolerror(conn):
    meeting_id = db.create_meeting(conn, "Silent meeting")

    with pytest.raises(ToolError, match="no transcript"):
        mcp_server.get_meeting_transcript(meeting_id)


def test_get_summary_prompt_returns_configured_template(conn):
    db.set_setting(conn, "summary_prompt_template", "Only action items.")
    assert mcp_server.get_summary_prompt() == "Only action items."


def test_get_summary_prompt_falls_back_to_default(conn):
    assert "key decisions" in mcp_server.get_summary_prompt().lower()


def test_save_meeting_summary_persists_with_mcp_provider(conn):
    meeting_id = db.create_meeting(conn, "Standup")

    message = mcp_server.save_meeting_summary(meeting_id, "We decided to ship.")

    rows = db.get_summaries(conn, meeting_id)
    assert len(rows) == 1
    assert rows[0]["content"] == "We decided to ship."
    assert rows[0]["provider"] == "mcp:claude-desktop"
    assert str(meeting_id) in message


def test_save_meeting_summary_unknown_meeting_raises_toolerror(conn):
    with pytest.raises(ToolError, match="999"):
        mcp_server.save_meeting_summary(999, "orphan summary")


def test_save_meeting_summary_rejects_empty_content(conn):
    meeting_id = db.create_meeting(conn, "Standup")

    with pytest.raises(ToolError, match="empty"):
        mcp_server.save_meeting_summary(meeting_id, "   ")


def test_stdio_server_exposes_all_four_tools_and_round_trips(tmp_path):
    """End-to-end over the real protocol: spawn the server, call tools, read the DB back.

    This is the only test that exercises the actual stdio transport, tool schema
    generation, and the ToolError-to-wire path.
    """
    db_path = tmp_path / "integration.db"
    setup_conn = db.connect(str(db_path))
    meeting_id = db.create_meeting(setup_conn, "Integration meeting")
    db.add_segment(setup_conn, meeting_id, "you", "let's ship on friday", 0.9)
    setup_conn.close()

    env = {**os.environ, "LIVESUBTITLE_DB_PATH": str(db_path)}
    params = StdioServerParameters(
        command=sys.executable, args=[str(REPO_ROOT / "mcp_server.py")], env=env
    )

    async def exercise():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = {t.name for t in tools.tools}

                transcript = await session.call_tool(
                    "get_meeting_transcript", {"meeting_id": meeting_id}
                )
                saved = await session.call_tool(
                    "save_meeting_summary",
                    {"meeting_id": meeting_id, "content": "Shipping Friday."},
                )
                missing = await session.call_tool(
                    "get_meeting_transcript", {"meeting_id": 4242}
                )
                return names, transcript, saved, missing

    names, transcript, saved, missing = asyncio.run(exercise())

    assert names == {
        "list_meetings",
        "get_meeting_transcript",
        "get_summary_prompt",
        "save_meeting_summary",
    }
    assert transcript.is_error is False
    assert "let's ship on friday" in "".join(
        block.text for block in transcript.content if block.type == "text"
    )
    assert saved.is_error is False

    # The spec requires unknown-id errors to carry text the client can relay.
    assert missing.is_error is True
    assert "4242" in "".join(
        block.text for block in missing.content if block.type == "text"
    )

    verify_conn = db.connect(str(db_path))
    rows = db.get_summaries(verify_conn, meeting_id)
    assert [r["content"] for r in rows] == ["Shipping Friday."]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_mcp_server.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'mcp_server'`.

- [ ] **Step 4: Implement the MCP server**

Create `mcp_server.py` at the repository root:

```python
"""Standalone MCP server exposing recorded meetings to an MCP client (e.g. Claude Desktop).

Claude Desktop launches this over stdio, so **stdout is the JSON-RPC transport**:
anything printed there corrupts the protocol and drops the connection. All
diagnostics go to stderr, and this module deliberately does not import
helper.main (which configures logging for the web service).

Reads and writes the same SQLite database the helper service uses, so it works
whether or not the helper is running. Set LIVESUBTITLE_DB_PATH to point at a
different database.
"""

import logging
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from helper import db

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

MCP_PROVIDER = "mcp:claude-desktop"

server = MCPServer(
    name="livesubtitle",
    instructions=(
        "Access to the user's locally recorded meeting transcripts. "
        "To summarize a meeting: call list_meetings to find it, "
        "get_meeting_transcript to read it, get_summary_prompt to see how the "
        "user wants summaries written, then save_meeting_summary to store the "
        "result so it appears in their dashboard."
    ),
)

_conn = None


def _get_conn():
    """Lazily open the shared database; tests replace the module-level _conn."""
    global _conn
    if _conn is None:
        db_path = os.getenv("LIVESUBTITLE_DB_PATH") or db.default_db_path()
        _conn = db.connect(db_path)
    return _conn


def _require_meeting(conn, meeting_id: int):
    meeting = db.get_meeting(conn, meeting_id)
    if meeting is None:
        raise ToolError(
            f"No meeting with id {meeting_id}. Call list_meetings to see valid ids."
        )
    return meeting


@server.tool()
def list_meetings() -> list[dict]:
    """List the user's recorded meetings, most recent first.

    Returns each meeting's id, title, start time, and end time (null if the
    meeting is still recording).
    """
    rows = db.list_meetings(_get_conn())
    logger.info("list_meetings -> %s meetings", len(rows))
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
        }
        for row in rows
    ]


@server.tool()
def get_meeting_transcript(meeting_id: int) -> dict:
    """Get the full speaker-labeled transcript of one meeting.

    Each segment is labeled "you" (the user, captured from their microphone) or
    "other" (everyone else, captured from the system audio output).
    """
    conn = _get_conn()
    meeting = _require_meeting(conn, meeting_id)
    rows = db.get_transcript(conn, meeting_id)
    if not rows:
        raise ToolError(
            f"Meeting {meeting_id} ({meeting['title']!r}) has no transcript segments — "
            "nothing was transcribed for it."
        )
    logger.info("get_meeting_transcript meeting_id=%s -> %s segments", meeting_id, len(rows))
    return {
        "meeting_id": meeting_id,
        "title": meeting["title"],
        "started_at": meeting["started_at"],
        "ended_at": meeting["ended_at"],
        "segments": [
            {
                "speaker": row["speaker"],
                "text": row["text"],
                "started_at": row["started_at"],
            }
            for row in rows
        ],
    }


@server.tool()
def get_summary_prompt() -> str:
    """Get the user's configured instructions for how meeting summaries should be written.

    Follow these instructions when summarizing, so summaries match what the user
    set up in their dashboard settings.
    """
    return db.get_all_settings(_get_conn())["summary_prompt_template"]


@server.tool()
def save_meeting_summary(meeting_id: int, content: str) -> str:
    """Save a generated summary for a meeting so it appears in the user's dashboard."""
    if not content.strip():
        raise ToolError("Refusing to save an empty summary.")
    conn = _get_conn()
    _require_meeting(conn, meeting_id)
    summary_id = db.add_summary(conn, meeting_id, MCP_PROVIDER, content)
    logger.info("save_meeting_summary meeting_id=%s summary_id=%s", meeting_id, summary_id)
    return f"Saved summary {summary_id} for meeting {meeting_id}."


if __name__ == "__main__":
    server.run()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_mcp_server.py -v`
Expected: PASS (11 unit tests + 1 integration test).

If the integration test fails with an import error inside the spawned subprocess, confirm `pyproject.toml` still contains `pythonpath = ["."]` and that the test spawns `mcp_server.py` by absolute path from `REPO_ROOT` — the subprocess needs the repo root on its path to `import helper`.

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py requirements.txt
git commit -m "feat: add stdio MCP server exposing meetings and summaries"
```

---

### Task 3: Surface summaries and the prompt template in the dashboard

**Files:**
- Modify: `helper/main.py`
- Modify: `helper/dashboard.html`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `db.get_summaries`, `db.get_meeting`, `db.DEFAULT_SETTINGS["summary_prompt_template"]` (Task 1); existing `GET /settings` / `PUT /settings` and `SettingsUpdate` (Phase 2).
- Produces: `GET /meetings/{meeting_id}/summaries` → `{"meeting_id": int, "summaries": [{id, meeting_id, provider, content, created_at}]}`; `PUT /settings` additionally accepts `summary_prompt_template`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_main.py` (keep all existing tests and the `isolated_state` fixture unchanged):

```python
def test_get_summaries_returns_saved_summaries():
    meeting_id = db.create_meeting(main.state.conn, "Standup")
    db.add_summary(main.state.conn, meeting_id, "mcp:claude-desktop", "We shipped.")

    with TestClient(main.app) as client:
        response = client.get(f"/meetings/{meeting_id}/summaries")

    assert response.status_code == 200
    body = response.json()
    assert body["meeting_id"] == meeting_id
    assert body["summaries"][0]["content"] == "We shipped."
    assert body["summaries"][0]["provider"] == "mcp:claude-desktop"


def test_get_summaries_empty_list_when_none_saved():
    meeting_id = db.create_meeting(main.state.conn, "Standup")

    with TestClient(main.app) as client:
        response = client.get(f"/meetings/{meeting_id}/summaries")

    assert response.status_code == 200
    assert response.json()["summaries"] == []


def test_get_summaries_for_missing_meeting_returns_404():
    with TestClient(main.app) as client:
        response = client.get("/meetings/999/summaries")

    assert response.status_code == 404


def test_settings_includes_summary_prompt_template():
    with TestClient(main.app) as client:
        response = client.get("/settings")

    assert "key decisions" in response.json()["summary_prompt_template"].lower()


def test_put_settings_updates_summary_prompt_template():
    with TestClient(main.app) as client:
        response = client.put(
            "/settings", json={"summary_prompt_template": "Only the action items."}
        )

    assert response.status_code == 200
    assert response.json()["summary_prompt_template"] == "Only the action items."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_main.py -v`
Expected: the new tests FAIL — 404s for the summaries endpoint (route does not exist) and a `KeyError`/assertion failure for the prompt template.

- [ ] **Step 3: Add the summaries endpoint and settings field**

In `helper/main.py`, add `summary_prompt_template` to the existing `SettingsUpdate` model (keep the three existing fields and the `whisper_model_size` validator exactly as they are):

```python
class SettingsUpdate(BaseModel):
    mic_device_id: Optional[str] = None
    stt_language: Optional[str] = None
    whisper_model_size: Optional[str] = None
    summary_prompt_template: Optional[str] = None
```

Then add this endpoint next to the existing `get_transcript` route:

```python
@app.get("/meetings/{meeting_id}/summaries")
def get_summaries(meeting_id: int):
    meeting = db.get_meeting(state.conn, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    rows = db.get_summaries(state.conn, meeting_id)
    return {"meeting_id": meeting_id, "summaries": [dict(r) for r in rows]}
```

No change is needed to `GET /settings` or the body of `PUT /settings` — both already pass the whole settings dict through, so the new key flows automatically once it is in `DEFAULT_SETTINGS` and `SettingsUpdate`.

- [ ] **Step 4: Render summaries and the prompt template in the dashboard**

In `helper/dashboard.html`, make three edits.

**(a)** Add a summaries container to the Meetings tab. Find this block:

```html
<section id="tab-meetings" class="tab-panel" data-tab="meetings">
  <div class="meeting-list" id="meetingList">Loading&hellip;</div>
  <div class="transcript-list" id="meetingTranscript"></div>
</section>
```

and replace it with:

```html
<section id="tab-meetings" class="tab-panel" data-tab="meetings">
  <div class="meeting-list" id="meetingList">Loading&hellip;</div>
  <div class="summary-list" id="meetingSummaries"></div>
  <div class="transcript-list" id="meetingTranscript"></div>
</section>
```

**(b)** Add styling for summaries. Add these rules to the `<style>` block, right after the existing `.meeting-row .meta` rule:

```css
    .summary-list { display: flex; flex-direction: column; gap: 10px; margin-top: 16px; }
    .summary-card {
      background: var(--surface);
      border: 1px solid var(--accent-dim, var(--border));
      border-left: 3px solid var(--accent);
      border-radius: var(--radius);
      padding: 12px 16px;
    }
    .summary-card .meta { color: var(--text-dim); font-size: 0.75rem; margin-bottom: 6px; }
    .summary-card .content { white-space: pre-wrap; line-height: 1.5; font-size: 0.9rem; }
    .summary-hint {
      color: var(--text-dim);
      font-size: 0.82rem;
      margin-top: 16px;
      line-height: 1.5;
    }
```

**(c)** Load summaries when a meeting is opened. Find the `loadTranscript` function and replace it with these two functions (this keeps the existing transcript behavior and adds the summaries fetch alongside it):

```js
  async function loadTranscript(meetingId) {
    loadSummaries(meetingId);
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

  async function loadSummaries(meetingId) {
    const container = document.getElementById('meetingSummaries');
    container.innerHTML = '';
    try {
      const response = await fetch(`/meetings/${meetingId}/summaries`);
      const data = await response.json();
      if (data.summaries.length === 0) {
        const hint = document.createElement('div');
        hint.className = 'summary-hint';
        hint.textContent =
          `No summary yet. Ask your MCP client (e.g. Claude Desktop) to summarize meeting ${meetingId}.`;
        container.appendChild(hint);
        return;
      }
      data.summaries.forEach((summary) => {
        const card = document.createElement('div');
        card.className = 'summary-card';
        const meta = document.createElement('div');
        meta.className = 'meta';
        meta.textContent = `${summary.provider} · ${summary.created_at}`;
        const content = document.createElement('div');
        content.className = 'content';
        content.textContent = summary.content;
        card.appendChild(meta);
        card.appendChild(content);
        container.appendChild(card);
      });
    } catch (err) {
      container.textContent = 'Could not load summaries.';
    }
  }
```

Note the summary card uses `textContent` rather than `innerHTML` for the provider and content — summary text is long-form model output and must not be interpreted as markup.

**(d)** Add the prompt template to the Settings form. Find the Whisper model `<div class="field">` block in the settings form and add this new field immediately after it, before the submit button:

```html
    <div class="field">
      <label for="promptTemplate">Summary prompt (used by your MCP client)</label>
      <textarea id="promptTemplate" rows="4"></textarea>
    </div>
```

Add this CSS rule next to the existing `.field select, .field input` rule (extend that selector rather than duplicating it):

```css
    .field select, .field input, .field textarea {
      width: 100%;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      padding: 8px 10px;
      font-size: 0.85rem;
    }
    .field textarea { resize: vertical; font-family: inherit; line-height: 1.5; }
```

(Delete the old `.field select, .field input { ... }` rule it replaces, so there is only one such block.)

In the JS, add a reference next to the other settings element lookups:

```js
  const promptTemplate = document.getElementById('promptTemplate');
```

In `loadSettings`, add this line next to the other three field assignments:

```js
    promptTemplate.value = settings.summary_prompt_template || '';
```

And in the settings form's submit handler, add the field to the `JSON.stringify` body (keep the three existing keys):

```js
        body: JSON.stringify({
          mic_device_id: micSelect.value,
          stt_language: languageInput.value || 'en',
          whisper_model_size: modelSelect.value,
          summary_prompt_template: promptTemplate.value,
        }),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_main.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add helper/main.py helper/dashboard.html tests/test_main.py
git commit -m "feat: show meeting summaries and edit summary prompt in dashboard"
```

---

### Task 4: Documentation and Claude Desktop setup instructions

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: documentation only, no runtime interfaces.

- [ ] **Step 1: Read the current CLAUDE.md**

Read `CLAUDE.md` in full first, so the new section matches the surrounding style and you place it correctly. The existing Phase 1/2 content lives under a `## Phase 1: meeting capture helper + overlay` heading that has since grown to cover the dashboard too.

- [ ] **Step 2: Add the Phase 3 section**

Add a new top-level section immediately after the existing Phase 1/2 content and before the `## Architecture notes` section:

````markdown
## Phase 3: MCP server (summarization via your own Claude subscription)

`mcp_server.py` is a third entry point, alongside the helper service and the overlay. Claude Desktop launches it over stdio; it reads and writes the same SQLite database, so the helper service does **not** need to be running for summarization to work.

**Tools it exposes:** `list_meetings`, `get_meeting_transcript`, `get_summary_prompt`, `save_meeting_summary`.

**Setup —** add this entry to the `mcpServers` object in `%APPDATA%\Claude\claude_desktop_config.json`, then fully restart Claude Desktop (quit from the tray, not just close the window):

```json
"livesubtitle": {
  "command": "C:\\Users\\wesle\\Desktop\\claude_code\\livesubtitle\\.venv\\Scripts\\python.exe",
  "args": ["C:\\Users\\wesle\\Desktop\\claude_code\\livesubtitle\\mcp_server.py"]
}
```

That file already contains other MCP servers — add this as one more key inside the existing `mcpServers` object, do not replace the object. Both paths must be absolute, and the `command` must be the project venv's Python (not a system Python), because the server imports `mcp` and the project's `helper` package.

**Usage:** ask Claude Desktop something like *"summarize my last meeting"*. It calls `list_meetings` → `get_meeting_transcript` → `get_summary_prompt`, writes the summary with `save_meeting_summary`, and the result appears in the dashboard's Meetings tab. The summary wording follows the **Summary prompt** field in the dashboard's Settings tab, so edit it there rather than repeating instructions to Claude each time.

**Environment:** set `LIVESUBTITLE_DB_PATH` to point the server at a database other than the default `%APPDATA%\livesubtitle\livesubtitle.db`.

**Debugging:** stdout is the JSON-RPC transport, so the server logs to stderr only and never prints. If Claude Desktop shows the server as failed, run it directly (`.venv\Scripts\python.exe mcp_server.py`) — it will sit waiting for input on stdin, which confirms it starts cleanly; import errors surface immediately.
````

- [ ] **Step 3: Run the full test suite**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: PASS (documentation-only change; this confirms nothing else drifted).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document Phase 3 MCP server and Claude Desktop setup"
```

- [ ] **Step 5: Manual verification — controller-only, not for a subagent**

This step requires editing the user's Claude Desktop config (a file outside this repo that already holds 8 working MCP server entries) and restarting their desktop app. **The controller must ask the user before touching that file.** Verification sequence once the user agrees: merge the `livesubtitle` entry into `mcpServers`, restart Claude Desktop, confirm the server appears connected, then ask Claude Desktop to summarize a real meeting and confirm the summary shows up in the dashboard's Meetings tab.
