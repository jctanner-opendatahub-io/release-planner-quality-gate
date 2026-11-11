"""Tests for the release-calendar sync script (no network access)."""
import json

import pytest

from scripts.update_release_calendar import (
    extract_events,
    merge_calendar,
    _parse_cookie_jar,
    _task_date,
)


def task(name, start, finish=None, flags=None, level=2, path=(), draft=False,
         invalid=False):
    """Build a synthetic Product Pages schedule row."""
    return {
        "name": name,
        "date_start": start,
        "date_finish": finish if finish is not None else start,
        "flags": list(flags or []),
        "level": level,
        "path": list(path),
        "draft": draft,
        "invalid": invalid,
    }


# ─── extract_events ──────────────────────────────────────────────────────────

def test_extract_events_rhai_37_shape():
    tasks = [
        task("3.7 EA1 RHOAI Planning Freeze", "2026-11-11"),
        task("3.7 EA1 RHOAI Code Freeze", "2026-12-11"),
        task("3.7 EA1 RHOAI RELEASE", "2027-01-20", flags=["ea", "jira-sync"],
             level=0),
        task("3.7 GA RHOAI Planning Freeze", "2026-12-11"),
        task("3.7 GA RHOAI Code Freeze", "2027-01-29"),
        task("3.7 GA RHOAI RELEASE", "2027-02-17", flags=["ga", "jira-sync"],
             level=0),
    ]
    events = extract_events(tasks, "RHOAI")
    assert events == {
        ("3.7", "EA1"): {
            "planningFreeze": "2026-11-11",
            "codeFreeze": "2026-12-11",
            "releaseDate": "2027-01-20",
        },
        ("3.7", "GA"): {
            "planningFreeze": "2026-12-11",
            "codeFreeze": "2027-01-29",
            "releaseDate": "2027-02-17",
        },
    }


def test_extract_events_ignores_other_product_lines():
    tasks = [
        task("3.7 EA1 RHOAI Code Freeze", "2026-12-11"),
        task("3.7 EA1 RHAII Code Freeze", "2026-12-01"),
        task("3.7 EA1 RHELAI Code Freeze", "2026-12-05"),
        task("3.7 EA1 AIPCC Code Freeze", "2026-12-02"),
        task("3.7 EA1 RHELAI RELEASE", "2027-01-20", flags=["ea"], level=0),
        task("3.7 [Fast-1] RHAII RELEASE", "2026-12-10",
             flags=["fast-1", "jira-sync"], level=0),
    ]
    events = extract_events(tasks, "RHOAI")
    assert list(events) == [("3.7", "EA1")]
    assert events[("3.7", "EA1")]["codeFreeze"] == "2026-12-11"


def test_extract_events_tolerates_double_space_names():
    # Real rows sometimes carry doubled whitespace: "3.5 GA  RHOAI Code Freeze".
    tasks = [task("3.5 GA  RHOAI Code Freeze", "2026-07-31")]
    events = extract_events(tasks, "RHOAI")
    assert events[("3.5", "GA")]["codeFreeze"] == "2026-07-31"


def test_extract_events_skips_draft_and_invalid_rows():
    tasks = [
        task("3.8 EA1 RHOAI Code Freeze", "2027-03-02", draft=True),
        task("3.8 EA1 RHOAI Code Freeze", "2027-03-09", flags=["draft"]),
        task("3.8 EA1 RHOAI Code Freeze", "2027-04-01", invalid=True),
        task("3.8 EA1 RHOAI Planning Freeze", "2027-02-03"),
    ]
    events = extract_events(tasks, "RHOAI")
    assert events == {("3.8", "EA1"): {"planningFreeze": "2027-02-03"}}


def test_extract_events_span_uses_last_day():
    tasks = [task("3.7 EA1 RHOAI Code Freeze", "2026-12-01", finish="2026-12-11")]
    events = extract_events(tasks, "RHOAI")
    assert events[("3.7", "EA1")]["codeFreeze"] == "2026-12-11"


def test_extract_events_prefers_top_level_jira_sync_release_row():
    tasks = [
        task("3.6 EA1 RHOAI RELEASE", "2026-09-23", flags=["ga", "jira-sync"],
             level=0),
        task("3.6 EA1 RHOAI RELEASE", "2026-09-30", flags=["ga", "roadmap"],
             level=2, path=["3.6 EA1 RHAI", "3.6 E"]),
    ]
    events = extract_events(tasks, "RHOAI")
    assert events[("3.6", "EA1")]["releaseDate"] == "2026-09-23"


def test_extract_events_ignores_unrelated_rows():
    tasks = [
        task("3.7 EA1 RHAI Component Version Freeze", "2026-11-20",
             flags=["freeze", "version"]),
        task("3.7 EA1 RHOAI Release Window", "2027-01-19", finish="2027-01-20",
             flags=["launch"]),
        task("3.7 EA1 RHAI RELEASE", "2027-01-20", flags=["launch"]),
        task("3.7 GA RHAII Partner Deliverables (Spyre)", "2027-01-15"),
        task("RC1 validation", "2027-01-22"),
    ]
    assert extract_events(tasks, "RHOAI") == {}


# ─── merge_calendar ──────────────────────────────────────────────────────────

@pytest.fixture
def calendar():
    return {
        "generatedAt": "2026-04-30",
        "note": "original note",
        "events": [
            {"version": "3.4", "event": "EA1",
             "planningFreeze": "2026-01-23", "codeFreeze": "2026-02-20",
             "releaseDate": "2026-03-19"},
            {"version": "3.6", "event": "GA",
             "planningFreeze": "2026-09-18", "codeFreeze": "2026-10-23",
             "releaseDate": "2026-11-19"},
        ],
    }


def test_merge_adds_new_events_and_preserves_untouched(calendar):
    new_events = {
        ("3.7", "EA1"): {"planningFreeze": "2026-11-11",
                         "codeFreeze": "2026-12-11",
                         "releaseDate": "2027-01-20"},
    }
    merged, changes = merge_calendar(calendar, new_events, ["rhai-3.7"], "RHOAI")
    versions = [event["version"] for event in merged["events"]]
    assert versions == ["3.4", "3.6", "3.7"]
    assert merged["note"] == "original note"
    assert any("added 3.7 EA1" in change for change in changes)
    # The 3.4 event (not present in Product Pages anymore) is untouched.
    assert merged["events"][0]["planningFreeze"] == "2026-01-23"


def test_merge_updates_changed_dates(calendar):
    new_events = {
        ("3.6", "GA"): {"planningFreeze": "2026-09-23",
                        "codeFreeze": "2026-10-30",
                        "releaseDate": "2026-12-02"},
    }
    merged, changes = merge_calendar(calendar, new_events, ["rhai-3.6"], "RHOAI")
    ga = next(e for e in merged["events"] if e["version"] == "3.6")
    assert ga["codeFreeze"] == "2026-10-30"
    assert ga["releaseDate"] == "2026-12-02"
    assert len(changes) == 3


def test_merge_null_never_overwrites_existing_value(calendar):
    new_events = {
        ("3.6", "GA"): {"planningFreeze": None, "codeFreeze": "2026-10-30",
                        "releaseDate": "2026-12-02"},
    }
    merged, changes = merge_calendar(calendar, new_events, ["rhai-3.6"], "RHOAI")
    ga = next(e for e in merged["events"] if e["version"] == "3.6")
    assert ga["planningFreeze"] == "2026-09-18"
    assert not any("planningFreeze" in change for change in changes)


def test_merge_skips_events_with_no_dates_at_all(calendar):
    new_events = {("3.9", "GA"): {"planningFreeze": None, "codeFreeze": None,
                                  "releaseDate": None}}
    merged, changes = merge_calendar(calendar, new_events, ["rhai-3.9"], "RHOAI")
    assert changes == []
    assert all(event["version"] != "3.9" for event in merged["events"])


def test_merge_records_sync_metadata(calendar):
    new_events = {("3.7", "GA"): {"codeFreeze": "2027-01-29"}}
    merged, _ = merge_calendar(calendar, new_events, ["rhai-3.7"], "RHOAI")
    assert merged["sync"]["source"] == \
        "productpages.redhat.com (Product Pages API v7)"
    assert merged["sync"]["releases"] == ["rhai-3.7"]
    assert merged["sync"]["productLine"] == "RHOAI"
    assert merged["generatedAt"] == merged["sync"]["ranAt"]
    # Sync metadata survives a second run (overwritten, not duplicated).
    merged2, _ = merge_calendar(merged, {}, ["rhai-3.7"], "RHOAI")
    assert merged2["sync"]["releases"] == ["rhai-3.7"]


def test_merge_sorts_events_by_version_and_event(calendar):
    new_events = {
        ("3.7", "GA"): {"codeFreeze": "2027-01-29"},
        ("3.7", "EA1"): {"codeFreeze": "2026-12-11"},
        ("3.5", "EA2"): {"codeFreeze": "2026-06-19"},
    }
    merged, _ = merge_calendar(calendar, new_events, ["rhai-3.5"], "RHOAI")
    order = [(e["version"], e["event"]) for e in merged["events"]]
    assert order == [
        ("3.4", "EA1"), ("3.5", "EA2"), ("3.6", "GA"),
        ("3.7", "EA1"), ("3.7", "GA"),
    ]


def test_merge_is_json_round_trippable(calendar):
    new_events = {("3.7", "EA1"): {"codeFreeze": "2026-12-11"}}
    merged, _ = merge_calendar(calendar, new_events, ["rhai-3.7"], "RHOAI")
    assert json.loads(json.dumps(merged)) == merged


# ─── helpers ─────────────────────────────────────────────────────────────────

def test_task_date_prefers_finish_then_start():
    assert _task_date({"date_start": "2026-12-01",
                       "date_finish": "2026-12-11"}) == "2026-12-11"
    assert _task_date({"date_start": "2026-12-01",
                       "date_finish": None}) == "2026-12-01"
    assert _task_date({"date_start": None, "date_finish": None}) is None


def test_parse_cookie_jar_filters_by_host(tmp_path):
    jar = tmp_path / "cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        "# https://curl.se/docs/http-cookies.html\n"
        "\n"
        ".redhat.com\tTRUE\t/\tTRUE\t1999999999\tsession\tabc123\n"
        "#HttpOnly_productpages.redhat.com\tFALSE\t/\tTRUE\t1999999999\tid\tdef456\n"
        "other.example.com\tFALSE\t/\tFALSE\t1999999999\tdecoy\txyz\n",
        encoding="utf-8")
    cookies = _parse_cookie_jar(str(jar), "productpages.redhat.com")
    assert cookies == {"session": "abc123", "id": "def456"}
