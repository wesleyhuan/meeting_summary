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
