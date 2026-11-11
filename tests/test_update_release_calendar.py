"""Tests for the release-calendar sync script (no network access)."""
import json
import urllib.error

import pytest

import scripts.update_release_calendar as upd
from scripts.update_release_calendar import (
    ProductPagesError,
    _event_sort_key,
    _merge_release_events,
    _parse_cookie_jar,
    _task_date,
    _task_level,
    extract_events,
    merge_calendar,
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


def test_extract_events_requires_jira_sync_for_release_rows():
    # A lone nested launch/roadmap RELEASE row must not become a releaseDate.
    tasks = [
        task("3.6 EA1 RHOAI RELEASE", "2026-09-30", flags=["ga", "roadmap"],
             level=2, path=["3.6 EA1 RHAI"]),
        task("3.6 EA1 RHOAI Code Freeze", "2026-08-31"),
    ]
    events = extract_events(tasks, "RHOAI")
    assert events == {("3.6", "EA1"): {"codeFreeze": "2026-08-31"}}


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


def test_extract_events_is_case_tolerant_on_kind_words():
    tasks = [
        task("3.7 EA1 RHOAI planning freeze", "2026-11-11"),
        task("3.7 EA1 RHOAI code freeze", "2026-12-11"),
    ]
    events = extract_events(tasks, "RHOAI")
    assert events[("3.7", "EA1")] == {"planningFreeze": "2026-11-11",
                                      "codeFreeze": "2026-12-11"}


def test_extract_events_skips_non_dict_rows(capsys):
    events = extract_events(["garbage", None, 42], "RHOAI")
    assert events == {}
    assert "skipping non-dict schedule row" in capsys.readouterr().err


def test_extract_events_skips_non_iso_dates(capsys):
    tasks = [
        task("3.7 EA1 RHOAI Code Freeze", "not-a-date"),
        task("3.7 EA1 RHOAI Planning Freeze", "2026-11-11"),
    ]
    events = extract_events(tasks, "RHOAI")
    assert events == {("3.7", "EA1"): {"planningFreeze": "2026-11-11"}}
    assert "non-ISO date" in capsys.readouterr().err


def test_task_date_normalizes_datetime_and_uses_span_end():
    assert _task_date({"date_start": "2026-12-11T00:00:00Z"}) == "2026-12-11"
    assert _task_date({"date_start": "2026-12-01",
                       "date_finish": "2026-12-11T00:00:00Z"}) == "2026-12-11"


def test_task_date_prefers_finish_then_start():
    assert _task_date({"date_start": "2026-12-01",
                       "date_finish": "2026-12-11"}) == "2026-12-11"
    assert _task_date({"date_start": "2026-12-01",
                       "date_finish": None}) == "2026-12-01"
    assert _task_date({"date_start": None, "date_finish": None}) is None


def test_task_level_missing_or_malformed_is_least_preferred():
    assert _task_level({"level": 0}) == 0
    assert _task_level({}) == upd._DEEPEST_LEVEL
    assert _task_level({"level": "top"}) == upd._DEEPEST_LEVEL
    # A row with a missing level must lose to a row with an explicit level.
    tasks = [
        {"name": "3.7 EA1 RHOAI Code Freeze", "date_start": "2026-12-01",
         "flags": [], "path": []},
        task("3.7 EA1 RHOAI Code Freeze", "2026-12-11", level=2),
    ]
    events = extract_events(tasks, "RHOAI")
    assert events[("3.7", "EA1")]["codeFreeze"] == "2026-12-11"


def test_extract_events_disagreement_warning_lists_all_conflicting_rows(capsys):
    tasks = [
        task("3.6 EA1 RHOAI RELEASE", "2026-09-23", flags=["ga", "jira-sync"],
             level=0),
        task("3.6 EA1 RHOAI RELEASE", "2026-09-30", flags=["ga", "jira-sync"],
             level=0),
        task("3.6 EA1 RHOAI RELEASE", "2026-10-07", flags=["ga", "jira-sync"],
             level=0),
    ]
    events = extract_events(tasks, "RHOAI")
    assert events[("3.6", "EA1")]["releaseDate"] == "2026-10-07"  # latest wins
    err = capsys.readouterr().err
    assert "2026-09-23" in err and "2026-09-30" in err  # both losers reported


# ─── cross-release merge ─────────────────────────────────────────────────────

def test_merge_release_events_authoritative_page_overrides():
    new_events = {("3.7", "GA"): {"codeFreeze": "2027-01-15"}}
    # rhai-3.7 page (authoritative) disagrees with an earlier spurious row.
    _merge_release_events(new_events, {("3.7", "GA"): {"codeFreeze": "2027-01-29"}},
                          "rhai-3.7", "rhai")
    assert new_events[("3.7", "GA")]["codeFreeze"] == "2027-01-29"


def test_merge_release_events_non_authoritative_page_only_fills_gaps(capsys):
    new_events = {("3.7", "GA"): {"codeFreeze": "2027-01-29",
                                   "planningFreeze": "2026-12-11"}}
    # A predecessor milestone on the rhai-3.8 page: must not override dates,
    # may fill a missing field, and must warn on disagreement.
    _merge_release_events(
        new_events,
        {("3.7", "GA"): {"codeFreeze": "2027-01-15", "releaseDate": "2027-02-17"}},
        "rhai-3.8", "rhai")
    assert new_events[("3.7", "GA")] == {"codeFreeze": "2027-01-29",
                                         "planningFreeze": "2026-12-11",
                                         "releaseDate": "2027-02-17"}
    assert "disagrees" in capsys.readouterr().err


def test_merge_release_events_first_page_creates_entries():
    new_events = {}
    _merge_release_events(new_events, {("3.5", "EA2"): {"codeFreeze": "2026-06-19"}},
                          "rhai-3.5", "rhai")
    assert new_events == {("3.5", "EA2"): {"codeFreeze": "2026-06-19"}}


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


def _event_changes(changes):
    return [c for c in changes if not c.startswith(("recorded ", "updated sync"))]


def test_merge_adds_new_events_and_preserves_untouched(calendar):
    new_events = {
        ("3.7", "EA1"): {"planningFreeze": "2026-11-11",
                         "codeFreeze": "2026-12-11",
                         "releaseDate": "2027-01-20"},
    }
    merged, changes = merge_calendar(calendar, new_events, ["rhai-3.7"], "RHOAI")
    versions = [event["version"] for event in merged["events"]]
    assert versions == ["3.4", "3.6", "3.7"]
    assert "original note" in merged["note"]  # preamble prepended, note kept
    assert any("added 3.7 EA1" in change for change in _event_changes(changes))
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
    event_changes = _event_changes(changes)
    assert len(event_changes) == 3
    assert all("updated 3.6 GA" in c for c in event_changes)


def test_merge_null_never_overwrites_existing_value(calendar):
    new_events = {
        ("3.6", "GA"): {"planningFreeze": None, "codeFreeze": "2026-10-30",
                        "releaseDate": "2026-12-02"},
    }
    merged, changes = merge_calendar(calendar, new_events, ["rhai-3.6"], "RHOAI")
    ga = next(e for e in merged["events"] if e["version"] == "3.6")
    assert ga["planningFreeze"] == "2026-09-18"
    assert not any("planningFreeze" in change for change in _event_changes(changes))


def test_merge_reports_vanished_milestones(calendar, capsys):
    # The fetch no longer carries planningFreeze for a fetched event that has
    # one: preserved, but loudly.
    new_events = {("3.6", "GA"): {"codeFreeze": "2026-10-30"}}
    merge_calendar(calendar, new_events, ["rhai-3.6"], "RHOAI")
    err = capsys.readouterr().err
    assert "did not report planningFreeze" in err
    assert "2026-09-18" in err


def test_merge_skips_events_with_no_dates_at_all(calendar):
    new_events = {("3.9", "GA"): {"planningFreeze": None, "codeFreeze": None,
                                  "releaseDate": None}}
    merged, changes = merge_calendar(calendar, new_events, ["rhai-3.9"], "RHOAI")
    assert _event_changes(changes) == []
    assert all(event["version"] != "3.9" for event in merged["events"])


def test_merge_dedupes_duplicate_calendar_rows(calendar, capsys):
    calendar["events"].append(dict(calendar["events"][1]))  # duplicate 3.6 GA
    merged, _ = merge_calendar(calendar, {}, [], "RHOAI")
    ga_rows = [e for e in merged["events"] if e["version"] == "3.6"]
    assert len(ga_rows) == 1
    assert "duplicate calendar row" in capsys.readouterr().err


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


def test_merge_sync_provenance_drift_is_a_change(calendar):
    merged, _ = merge_calendar(calendar, {}, ["rhai-3.7"], "RHOAI")
    # Same calendar, later scoped run: provenance must converge via a change.
    merged2, changes = merge_calendar(merged, {}, ["rhai-3.5", "rhai-3.6",
                                                   "rhai-3.7"], "RHOAI")
    assert any("updated sync provenance" in c for c in changes)
    assert merged2["sync"]["releases"] == ["rhai-3.5", "rhai-3.6", "rhai-3.7"]


def test_merge_first_sync_prepends_provenance_note(calendar):
    merged, changes = merge_calendar(calendar, {}, ["rhai-3.7"], "RHOAI")
    assert merged["note"].startswith("Dates for synced events come from "
                                      "Product Pages")
    assert merged["note"].endswith("original note")
    assert any("recorded Product Pages provenance" in c for c in changes)
    # Second sync: note is not prepended again.
    merged2, changes2 = merge_calendar(merged, {}, ["rhai-3.7"], "RHOAI")
    assert merged2["note"] == merged["note"]
    assert not any("provenance" in c for c in changes2)


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


def test_event_sort_key_orders_ea_numerically_and_ga_last():
    events = [{"version": "3.7", "event": label}
              for label in ("GA", "EA10", "EA2", "EA1")]
    ordered = [e["event"] for e in sorted(events, key=_event_sort_key)]
    assert ordered == ["EA1", "EA2", "EA10", "GA"]


def test_merge_is_json_round_trippable(calendar):
    new_events = {("3.7", "EA1"): {"codeFreeze": "2026-12-11"}}
    merged, _ = merge_calendar(calendar, new_events, ["rhai-3.7"], "RHOAI")
    assert json.loads(json.dumps(merged)) == merged


# ─── helpers ─────────────────────────────────────────────────────────────────

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


# ─── security / network layer ────────────────────────────────────────────────

def test_base_url_must_be_https():
    with pytest.raises(ProductPagesError, match="HTTPS"):
        upd._validate_base_url("http://productpages.redhat.com")


def test_base_url_host_must_be_approved(monkeypatch):
    monkeypatch.delenv("PRODUCTPAGES_ALLOWED_HOSTS", raising=False)
    with pytest.raises(ProductPagesError, match="not an approved"):
        upd._validate_base_url("https://evil.example.com")
    monkeypatch.setenv("PRODUCTPAGES_ALLOWED_HOSTS", "evil.example.com")
    assert upd._validate_base_url("https://evil.example.com") == "evil.example.com"


def test_base_url_must_not_carry_path():
    with pytest.raises(ProductPagesError, match="path"):
        upd._validate_base_url("https://productpages.redhat.com/evil")


def test_bearer_token_with_internal_whitespace_is_rejected(monkeypatch):
    secret = "SECRET\r\ninjected"
    monkeypatch.setenv("PRODUCTPAGES_TOKEN", secret)
    with pytest.raises(ProductPagesError) as excinfo:
        upd.authenticate("https://productpages.redhat.com")
    assert "SECRET" not in str(excinfo.value)


def test_bearer_token_is_stripped(monkeypatch):
    monkeypatch.setenv("PRODUCTPAGES_TOKEN", "  tok123\n")
    auth = upd.authenticate("https://productpages.redhat.com")
    assert auth.bearer == "tok123"


def test_kerberos_refuses_unexpected_login_redirect(monkeypatch, tmp_path):
    calls = {}

    def fake_curl(args):
        calls["args"] = args
        if "-D" in args:
            header_file = args[args.index("-D") + 1]
            with open(header_file, "w") as f:
                f.write("HTTP/2 303\nlocation: https://evil.example.com/login\n")
            return ""
        return "303"

    monkeypatch.setattr(upd, "_curl", fake_curl)
    with pytest.raises(ProductPagesError, match="unexpected URL host"):
        upd._authenticate_kerberos("https://productpages.redhat.com")


def test_http_json_sanitizes_header_value_errors(monkeypatch):
    secret = "Bearer SUPERSECRET"

    class BadOpener:
        def open(self, request, timeout=None):
            raise ValueError(f"Invalid header value b'{secret}'")

    monkeypatch.setattr(urllib.request, "build_opener", lambda handler: BadOpener())
    with pytest.raises(ProductPagesError) as excinfo:
        upd._http_json("https://productpages.redhat.com/api/v7/whoami/",
                       upd.Auth(bearer="SUPERSECRET"))
    assert "SUPERSECRET" not in str(excinfo.value)


def test_http_json_wraps_network_errors(monkeypatch):
    class BadOpener:
        def open(self, request, timeout=None):
            raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(urllib.request, "build_opener", lambda handler: BadOpener())
    with pytest.raises(ProductPagesError, match="network error"):
        upd._http_json("https://productpages.redhat.com/api/v7/whoami/", upd.Auth())


def test_form_post_wraps_network_errors():
    # .invalid never resolves; must surface as ProductPagesError, not a traceback.
    with pytest.raises(ProductPagesError):
        upd._form_post("https://no-such-host.invalid/token",
                       {"grant_type": "client_credentials",
                        "client_id": "x", "client_secret": "y"})


def test_api_get_rejects_off_origin_pagination_url():
    with pytest.raises(ProductPagesError, match="off-origin"):
        upd.api_get("https://productpages.redhat.com", upd.Auth(),
                    "https://evil.example.com/api/v7/next")


def test_fetch_releases_follows_drf_pagination(monkeypatch):
    pages = [
        {"results": [{"shortname": "rhai-3.7"}, {"shortname": "rhai-Z-Stream"}],
         "next": "https://productpages.redhat.com/api/v7/releases/?cursor=2"},
        {"results": [{"shortname": "rhai-3.5"}], "next": None},
    ]
    calls = []

    def fake_api_get(base_url, auth, path, params=None):
        calls.append(path)
        return pages[len(calls) - 1]

    monkeypatch.setattr(upd, "api_get", fake_api_get)
    releases = upd.fetch_releases("https://productpages.redhat.com",
                                  upd.Auth(), "rhai")
    assert releases == ["rhai-3.5", "rhai-3.7"]  # Z-Stream excluded, sorted
    assert len(calls) == 2


def test_fetch_releases_rejects_unexpected_shape(monkeypatch):
    monkeypatch.setattr(upd, "api_get", lambda *a, **k: {"unexpected": True})
    with pytest.raises(ProductPagesError, match="response shape"):
        upd.fetch_releases("https://productpages.redhat.com", upd.Auth(), "rhai")


def test_fetch_schedule_tasks_quotes_release_shortname(monkeypatch):
    seen = {}

    def fake_api_get(base_url, auth, path, params=None):
        seen["path"] = path
        return []

    monkeypatch.setattr(upd, "api_get", fake_api_get)
    upd.fetch_schedule_tasks("https://productpages.redhat.com", upd.Auth(),
                             "rhai 3.7/x")
    assert seen["path"] == "releases/rhai%203.7%2Fx/schedule-tasks/"


def test_write_calendar_is_atomic_and_survives_replace_failure(tmp_path, monkeypatch):
    calendar_path = tmp_path / "release-calendar.json"
    calendar_path.write_text('{"events": []}\n', encoding="utf-8")
    original = calendar_path.read_text()

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(upd.os, "replace", failing_replace)
    with pytest.raises(OSError):
        upd._write_calendar(str(calendar_path), {"events": [1]})
    assert calendar_path.read_text() == original  # untouched
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".release-calendar-")]
    assert leftovers == []  # temp file cleaned up


# ─── CLI (main) ──────────────────────────────────────────────────────────────

def _stub_network(monkeypatch, releases=None, schedule=None):
    monkeypatch.setattr(upd, "authenticate",
                        lambda base_url: upd.Auth(bearer="stub"))
    monkeypatch.setattr(upd, "whoami", lambda base_url, auth: "tester")
    monkeypatch.setattr(upd, "fetch_releases",
                        lambda base_url, auth, product: releases or [])
    monkeypatch.setattr(upd, "fetch_schedule_tasks",
                        lambda base_url, auth, release: schedule or [])


def test_main_rejects_invalid_release_shortname(monkeypatch, tmp_path):
    _stub_network(monkeypatch)
    with pytest.raises(SystemExit):
        upd.main(["--calendar", str(tmp_path / "c.json"),
                  "--releases", "rhai-3.7/../../etc"])


def test_main_dry_run_writes_nothing(monkeypatch, tmp_path, capsys):
    _stub_network(monkeypatch, schedule=[
        task("3.7 EA1 RHOAI Code Freeze", "2026-12-11"),
        task("3.7 EA1 RHOAI Planning Freeze", "2026-11-11"),
        task("3.7 EA1 RHOAI RELEASE", "2027-01-20",
             flags=["ea", "jira-sync"], level=0),
    ])
    calendar_path = tmp_path / "release-calendar.json"
    calendar_path.write_text(json.dumps({"note": "n", "events": []}), encoding="utf-8")
    original = calendar_path.read_text()
    rc = upd.main(["--calendar", str(calendar_path), "--releases", "rhai-3.7",
                   "--dry-run"])
    assert rc == 0
    assert "Dry run:" in capsys.readouterr().out
    assert calendar_path.read_text() == original


def test_main_zero_extraction_exits_2(monkeypatch, tmp_path, capsys):
    _stub_network(monkeypatch, schedule=[])  # nothing recognizable
    calendar_path = tmp_path / "release-calendar.json"
    calendar_path.write_text(json.dumps({"note": "n", "events": []}), encoding="utf-8")
    original = calendar_path.read_text()
    rc = upd.main(["--calendar", str(calendar_path), "--releases", "rhai-3.7"])
    assert rc == 2
    assert "no matching schedule events" in capsys.readouterr().err
    assert calendar_path.read_text() == original  # not updated


def test_main_allow_empty_downgrades_to_success(monkeypatch, tmp_path, capsys):
    _stub_network(monkeypatch, schedule=[])
    calendar_path = tmp_path / "release-calendar.json"
    calendar_path.write_text(json.dumps({
        "note": "n", "events": [],
        "sync": {"productLine": "RHOAI", "releases": ["rhai-3.7"]},
    }), encoding="utf-8")
    rc = upd.main(["--calendar", str(calendar_path), "--releases", "rhai-3.7",
                   "--allow-empty"])
    assert rc == 0
    assert "already up to date" in capsys.readouterr().out


def test_main_product_line_guard(monkeypatch, tmp_path):
    _stub_network(monkeypatch)
    calendar_path = tmp_path / "release-calendar.json"
    calendar_path.write_text(json.dumps({
        "note": "n", "events": [],
        "sync": {"productLine": "RHOAI", "releases": ["rhai-3.7"]},
    }), encoding="utf-8")
    with pytest.raises(ProductPagesError, match="force-product-line"):
        upd.main(["--calendar", str(calendar_path), "--releases", "rhai-3.7",
                  "--product-line", "RHELAI"])
    # The override flag proceeds (network is stubbed; zero events → exit 2).
    rc = upd.main(["--calendar", str(calendar_path), "--releases", "rhai-3.7",
                   "--product-line", "RHELAI", "--force-product-line",
                   "--allow-empty"])
    assert rc == 0


def test_main_validates_product_line_choices(monkeypatch, tmp_path):
    _stub_network(monkeypatch)
    with pytest.raises(SystemExit):
        upd.main(["--calendar", str(tmp_path / "c.json"),
                  "--product-line", "rhoai"])


def test_main_reads_existing_calendar_before_network(monkeypatch, tmp_path):
    # A corrupted calendar must fail before any network call happens.
    def boom(*args, **kwargs):
        raise AssertionError("network should not be reached")

    monkeypatch.setattr(upd, "authenticate", boom)
    calendar_path = tmp_path / "release-calendar.json"
    calendar_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProductPagesError, match="not valid JSON"):
        upd.main(["--calendar", str(calendar_path), "--releases", "rhai-3.7"])
