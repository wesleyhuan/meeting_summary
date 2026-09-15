import asyncio
import os
import sqlite3
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


def test_get_conn_unexpected_connect_failure_surfaces_as_tool_error(monkeypatch):
    """A bad LIVESUBTITLE_DB_PATH (unwritable dir, corrupt file, etc.) fails
    lazily inside db.connect() the first time a tool runs; that failure must
    not reach the client as an opaque, unlogged exception either."""

    def boom(path):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(db, "connect", boom)

    with pytest.raises(ToolError, match="unable to open database file"):
        mcp_server._get_conn()


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
    # Anchored match: guards against the deliberate "No meeting with id" error
    # ever being double-wrapped into "Database error during ...: No meeting...".
    with pytest.raises(ToolError, match=r"^No meeting with id 999"):
        mcp_server.get_meeting_transcript(999)


def test_get_meeting_transcript_unexpected_db_error_surfaces_as_tool_error(
    conn, monkeypatch, caplog
):
    """An unexpected sqlite failure must reach the client as a ToolError that
    names what failed and includes the underlying message (which the MCP SDK
    would otherwise replace with opaque generic text), and must be logged."""
    meeting_id = db.create_meeting(conn, "Standup")

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "get_transcript", boom)

    with caplog.at_level("ERROR"):
        with pytest.raises(
            ToolError, match=r"Database error during fetching transcript.*locked"
        ):
            mcp_server.get_meeting_transcript(meeting_id)

    assert any(r.levelname == "ERROR" and r.exc_info for r in caplog.records)


def test_get_meeting_transcript_empty_transcript_raises_toolerror(conn):
    meeting_id = db.create_meeting(conn, "Silent meeting")

    with pytest.raises(ToolError, match="no transcript"):
        mcp_server.get_meeting_transcript(meeting_id)


def test_get_summary_prompt_returns_configured_template(conn):
    db.set_setting(conn, "summary_prompt_template", "Only action items.")
    assert mcp_server.get_summary_prompt() == "Only action items."


def test_get_summary_prompt_falls_back_to_default(conn):
    assert "key decisions" in mcp_server.get_summary_prompt().lower()


def test_get_summary_prompt_falls_back_to_default_when_stored_value_is_blank(conn):
    """A saved-but-empty template (cleared Settings field, or a failed
    /settings load that persisted "") must not hand Claude Desktop an empty
    instruction."""
    db.set_setting(conn, "summary_prompt_template", "")
    assert mcp_server.get_summary_prompt() == db.DEFAULT_SETTINGS["summary_prompt_template"]

    db.set_setting(conn, "summary_prompt_template", "   ")
    assert mcp_server.get_summary_prompt() == db.DEFAULT_SETTINGS["summary_prompt_template"]


def test_save_meeting_summary_persists_with_mcp_provider(conn):
    meeting_id = db.create_meeting(conn, "Standup")

    message = mcp_server.save_meeting_summary(meeting_id, "We decided to ship.")

    rows = db.get_summaries(conn, meeting_id)
    assert len(rows) == 1
    assert rows[0]["content"] == "We decided to ship."
    assert rows[0]["provider"] == "mcp:claude-desktop"
    assert str(meeting_id) in message


def test_save_meeting_summary_unknown_meeting_raises_toolerror(conn):
    with pytest.raises(ToolError, match=r"^No meeting with id 999"):
        mcp_server.save_meeting_summary(999, "orphan summary")


def test_save_meeting_summary_unexpected_db_error_surfaces_as_tool_error(conn, monkeypatch):
    meeting_id = db.create_meeting(conn, "Standup")

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "add_summary", boom)

    with pytest.raises(ToolError, match=r"Database error during saving summary.*locked"):
        mcp_server.save_meeting_summary(meeting_id, "We decided to ship.")


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
