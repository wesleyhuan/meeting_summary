import pytest

import mcp_server


@pytest.fixture(autouse=True)
def _isolate_mcp_db(tmp_path, monkeypatch):
    """Guard against a future un-fixtured test reaching the production database.

    mcp_server._get_conn() falls back to helper.db.default_db_path()
    (%APPDATA%\\livesubtitle\\livesubtitle.db) whenever LIVESUBTITLE_DB_PATH is
    unset and mcp_server._conn hasn't already been set by a test fixture. Point
    every test at a throwaway path by default, and reset the module's cached
    connection so a connection opened by an earlier test can't paper over a
    missing fixture in a later one.

    Tests that set LIVESUBTITLE_DB_PATH explicitly (e.g. the stdio integration
    test's subprocess env, built as {**os.environ, "LIVESUBTITLE_DB_PATH": ...})
    still win, since their explicit value is applied after copying os.environ.
    """
    monkeypatch.setenv("LIVESUBTITLE_DB_PATH", str(tmp_path / "unused.db"))
    monkeypatch.setattr(mcp_server, "_conn", None)
