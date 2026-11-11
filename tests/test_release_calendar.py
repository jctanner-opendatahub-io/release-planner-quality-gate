"""Tests for discovery Target Version resolution from the release calendar."""
import os

from datetime import date

from scripts.release_calendar import (
    expand_jira_names_for_future_events,
    expand_jira_target_version_names,
    is_obsolete_version_name,
    load_calendar,
    resolve_discovery_target_versions,
    versions_with_future_code_freeze,
)


SAMPLE_CALENDAR = {
    "events": [
        {
            "version": "3.4",
            "event": "GA",
            "codeFreeze": "2026-04-15",
        },
        {
            "version": "3.5",
            "event": "EA1",
            "codeFreeze": "2026-05-15",
        },
        {
            "version": "3.5",
            "event": "GA",
            "codeFreeze": "2026-07-24",
        },
        {
            "version": "3.6",
            "event": "EA1",
            "codeFreeze": "2026-08-21",
        },
        {
            "version": "3.6",
            "event": "EA2",
            "codeFreeze": "2026-09-18",
        },
        {
            "version": "3.6",
            "event": "GA",
            "codeFreeze": "2026-10-23",
        },
        {
            "version": "rhoai-3.6",
            "event": "EA1",
            "codeFreeze": "2026-08-21",
        },
        {
            "version": "3.6.1",
            "event": "GA",
            "codeFreeze": "2026-12-01",
        },
        {
            "version": "bad",
            "event": "GA",
            "codeFreeze": "2026-12-01",
        },
        {
            "version": "3.7",
            "event": "EA1",
            "codeFreeze": "not-a-date",
        },
    ]
}


class TestVersionsWithFutureCodeFreeze:
    def test_selects_versions_with_any_future_freeze(self):
        versions = versions_with_future_code_freeze(
            SAMPLE_CALENDAR, as_of=date(2026, 8, 14)
        )
        assert versions == ["3.6", "3.6.1"]

    def test_includes_freeze_on_as_of_day(self):
        versions = versions_with_future_code_freeze(
            SAMPLE_CALENDAR, as_of=date(2026, 8, 21)
        )
        assert "3.6" in versions

    def test_excludes_obsolete_rhoai_prefix(self):
        versions = versions_with_future_code_freeze(
            SAMPLE_CALENDAR, as_of=date(2026, 1, 1)
        )
        assert "rhoai-3.6" not in versions
        assert "bad" not in versions
        assert "3.7" not in versions

    def test_sorted_numerically(self):
        versions = versions_with_future_code_freeze(
            SAMPLE_CALENDAR, as_of=date(2026, 1, 1)
        )
        assert versions == ["3.4", "3.5", "3.6", "3.6.1"]


class TestExpandJiraTargetVersionNames:
    def test_expands_bare_cycle_to_product_family_events(self):
        names = expand_jira_target_version_names(["3.6"], SAMPLE_CALENDAR)
        assert "3.6 EA1 RHOAI RELEASE" in names
        assert "3.6 EA1 RHAII RELEASE" in names
        assert "3.6 EA1 RHELAI RELEASE" in names
        assert "3.6 GA RHOAI RELEASE" in names
        assert "rhoai-3.6" not in names
        assert "3.6" not in names

    def test_uses_calendar_events_only(self):
        names = expand_jira_target_version_names(["3.5"], SAMPLE_CALENDAR)
        assert "3.5 EA1 RHOAI RELEASE" in names
        assert "3.5 GA RHOAI RELEASE" in names
        # SAMPLE_CALENDAR has no 3.5 EA2 row
        assert "3.5 EA2 RHOAI RELEASE" not in names

    def test_as_of_filters_already_frozen_events(self):
        # Day after 3.6 EA1 freeze: EA1 omitted; EA2/GA kept.
        names = expand_jira_target_version_names(
            ["3.6"], SAMPLE_CALENDAR, as_of=date(2026, 8, 22)
        )
        assert "3.6 EA1 RHOAI RELEASE" not in names
        assert "3.6 EA2 RHOAI RELEASE" in names
        assert "3.6 GA RHOAI RELEASE" in names


class TestExpandJiraNamesForFutureEvents:
    def test_omits_frozen_events_within_open_cycle(self):
        names = expand_jira_names_for_future_events(
            SAMPLE_CALENDAR, as_of=date(2026, 8, 22)
        )
        assert "3.6 EA1 RHOAI RELEASE" not in names
        assert "3.6 EA2 RHOAI RELEASE" in names
        assert "3.6.1 GA RHOAI RELEASE" in names


class TestResolveDiscoveryTargetVersions:
    def test_explicit_bare_versions_expand(self, tmp_path):
        calendar_path = tmp_path / "cal.json"
        calendar_path.write_text(
            '{"events":[{"version":"3.6","event":"EA1","codeFreeze":"2026-08-21"},'
            '{"version":"3.6","event":"GA","codeFreeze":"2026-10-23"}]}'
        )
        config = {
            "jql": {
                "target_versions": ["3.6", "rhoai-3.5"],
                "target_versions_from_calendar": True,
                "calendar_path": str(calendar_path),
            }
        }
        names = resolve_discovery_target_versions(
            config, as_of=date(2026, 8, 14))
        assert "3.6 EA1 RHOAI RELEASE" in names
        assert "3.6 GA RHELAI RELEASE" in names
        assert "rhoai-3.5" not in names
        assert "3.6" not in names

    def test_explicit_picklist_names_kept(self):
        config = {
            "jql": {
                "target_versions": [
                    "3.6 EA1 RHOAI RELEASE",
                    "rhoai-3.6",
                ],
                "target_versions_from_calendar": True,
            }
        }
        assert resolve_discovery_target_versions(config) == [
            "3.6 EA1 RHOAI RELEASE"
        ]

    def test_calendar_disabled_returns_empty_without_explicit(self):
        config = {
            "jql": {
                "target_versions_from_calendar": False,
            }
        }
        assert resolve_discovery_target_versions(config) == []

    def test_missing_calendar_file_raises(self, tmp_path):
        config = {
            "jql": {
                "target_versions_from_calendar": True,
                "calendar_path": str(tmp_path / "missing.json"),
            }
        }
        try:
            resolve_discovery_target_versions(config)
            raise AssertionError("expected FileNotFoundError")
        except FileNotFoundError:
            pass

    def test_calendar_path_omits_frozen_events(self, tmp_path):
        calendar_path = tmp_path / "cal.json"
        calendar_path.write_text(
            '{"events":['
            '{"version":"3.6","event":"EA1","codeFreeze":"2026-08-21"},'
            '{"version":"3.6","event":"EA2","codeFreeze":"2026-09-18"}'
            ']}'
        )
        config = {
            "jql": {
                "target_versions_from_calendar": True,
                "calendar_path": str(calendar_path),
            }
        }
        names = resolve_discovery_target_versions(
            config, as_of=date(2026, 8, 22)
        )
        assert "3.6 EA1 RHOAI RELEASE" not in names
        assert "3.6 EA2 RHOAI RELEASE" in names

    def test_loads_repo_calendar_when_enabled(self, tmp_path):
        calendar_path = tmp_path / "cal.json"
        calendar_path.write_text(
            '{"events":[{"version":"3.9","event":"EA1","codeFreeze":"2027-01-01"}]}'
        )
        config = {
            "jql": {
                "target_versions_from_calendar": True,
                "calendar_path": str(calendar_path),
            }
        }
        names = resolve_discovery_target_versions(
            config, as_of=date(2026, 8, 14)
        )
        assert "3.9 EA1 RHOAI RELEASE" in names
        assert "3.9 EA1 RHAII RELEASE" in names
        assert is_obsolete_version_name("rhoai-3.9")


def test_repo_calendar_has_code_freeze_beyond_30_days():
    """Guard against the silent fail-closed outage (PR #12 postmortem).

    Discovery only emits Target Versions whose code freeze is still in the
    future; when the calendar runs out, every batch run evaluates zero
    issues and exits 0. Require at least one code freeze ≥ 30 days out so
    the calendar expiring turns CI red before the gate goes dark.
    """
    from datetime import timedelta

    repo_calendar = os.path.join(
        os.path.dirname(__file__), "..", "config", "release-calendar.json")
    calendar = load_calendar(repo_calendar)
    horizon = date.today() + timedelta(days=30)
    assert versions_with_future_code_freeze(calendar, as_of=horizon), (
        "config/release-calendar.json has no code freeze ≥ 30 days out — "
        "QG1 discovery is about to fail closed; run "
        "scripts/update_release_calendar.py and commit the refreshed calendar")
