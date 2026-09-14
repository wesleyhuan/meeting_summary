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


def test_add_segment_uses_provided_started_at(conn):
    meeting_id = db.create_meeting(conn, "Standup")
    db.add_segment(conn, meeting_id, "you", "hello", 0.9, started_at="2020-01-01T00:00:00+00:00")
    row = db.get_transcript(conn, meeting_id)[0]
    assert row["started_at"] == "2020-01-01T00:00:00+00:00"


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
