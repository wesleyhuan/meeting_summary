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
