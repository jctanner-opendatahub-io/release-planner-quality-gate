"""Loud-failure guards: credential preflight and zero-discovery anomalies.

Background: QG1 ran green-but-empty twice in production (2026-11-15/16)
because Jira Cloud's search endpoint silently degrades invalid Basic
credentials to anonymous — HTTP 200 with zero rows, not 401 — and the
empty-discovery path exited 0. The same silent path also hid the
stale-release-calendar window (2026-10-24 → 2026-11-11). These tests pin
the guards that convert both failure classes into loud, non-zero exits.
"""
import urllib.error
from unittest.mock import patch

import pytest
import scripts.quality_gate as qg

from scripts import jira_utils


def _http_error(code):
    return urllib.error.HTTPError(
        "https://jira.example/rest/api/3/myself", code, "err", {}, None
    )


def _write_config(tmp_path, calendar_path=None, target_versions=None):
    """Minimal pipeline-settings.yaml; explicit TVs keep tests clock-free."""
    settings = {
        "jql": {
            "scopes": [
                {"project": "RHAISTRAT", "issuetypes": ["Feature"]},
            ],
            "excluded_statuses": ["Closed", "Resolved", "Cancelled"],
            "skip_labels": [],
        },
        "labels": {
            "gate_pass": "rp-qg1-pass",
            "gate_fail": "rp-qg1-fail",
            "auto_rice": "rp-qg1-auto-rice",
        },
        "checks": {"hard_checks": []},
    }
    if target_versions is not None:
        settings["jql"]["target_versions"] = target_versions
    else:
        settings["jql"]["target_versions_from_calendar"] = True
        settings["jql"]["calendar_path"] = str(calendar_path)
    cfg = tmp_path / "pipeline-settings.yaml"
    cfg.write_text(
        __import__("yaml").safe_dump(settings), encoding="utf-8"
    )
    return str(cfg)


def _expired_calendar(tmp_path):
    """Calendar whose only event has a permanently past code freeze."""
    cal = tmp_path / "release-calendar.json"
    cal.write_text(
        __import__("json").dumps(
            {
                "generatedAt": "2026-01-01",
                "events": [
                    {
                        "version": "3.4",
                        "event": "EA1",
                        "planningFreeze": "2026-01-23",
                        "codeFreeze": "2026-02-20",
                        "releaseDate": "2026-03-19",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return str(cal)


@pytest.fixture
def jira_env(monkeypatch):
    monkeypatch.setenv("JIRA_SERVER", "https://jira.example")
    monkeypatch.setenv("JIRA_USER", "bot@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "token-value")


# --- Credential preflight ---


class TestVerifyCredentials:
    def test_rejected_credentials_exit_2(self, capsys):
        with patch.object(
            jira_utils,
            "api_call_with_retry",
            side_effect=_http_error(401),
        ):
            with pytest.raises(SystemExit) as excinfo:
                jira_utils.verify_credentials(
                    "https://jira.example", "bot@example.com", "token-value"
                )
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "PREFLIGHT FAIL" in err
        assert "silently return zero issues" in err

    def test_valid_credentials_do_not_exit(self):
        with patch.object(
            jira_utils, "api_call_with_retry", return_value={}
        ):
            # No exception, no SystemExit.
            jira_utils.verify_credentials(
                "https://jira.example", "bot@example.com", "token-value"
            )

    def test_outage_errors_reraise_not_credential_exit(self):
        # A 503 that survives the retry cycle is a Jira outage, not a
        # rejected credential: it must propagate, not exit 2.
        with patch.object(
            jira_utils,
            "api_call_with_retry",
            side_effect=_http_error(503),
        ):
            with pytest.raises(urllib.error.HTTPError):
                jira_utils.verify_credentials(
                    "https://jira.example", "bot@example.com", "token-value"
                )


# --- Zero-discovery guards in main() ---


class TestMainZeroDiscovery:
    def _run_main(self, argv):
        # Credentials are preflight-verified in main(); tests stub that out
        # (its HTTP behaviour is covered above) along with discovery and
        # artifact writes, so only the exit logic is exercised.
        with patch.object(qg, "verify_credentials"), patch.object(
            qg, "write_artifacts"
        ):
            qg.main(argv)

    def test_zero_issues_with_live_calendar_exits_3(
        self, jira_env, tmp_path, capsys
    ):
        cfg = _write_config(
            tmp_path, target_versions=["3.9 EA1 RHOAI RELEASE"]
        )
        with patch.object(qg, "discover_issues", return_value=[]):
            with pytest.raises(SystemExit) as excinfo:
                self._run_main(["--config", cfg])
        assert excinfo.value.code == 3
        err = capsys.readouterr().err
        assert "ANOMALY" in err
        assert "3.9 EA1 RHOAI RELEASE" in err

    def test_zero_issues_with_expired_calendar_exits_4(
        self, jira_env, tmp_path, capsys
    ):
        cfg = _write_config(tmp_path, calendar_path=_expired_calendar(tmp_path))
        with patch.object(qg, "discover_issues", return_value=[]):
            with pytest.raises(SystemExit) as excinfo:
                self._run_main(["--config", cfg])
        assert excinfo.value.code == 4
        err = capsys.readouterr().err
        assert "CALENDAR EXPIRED" in err

    def test_single_issue_not_found_exits_5(
        self, jira_env, tmp_path, capsys
    ):
        cfg = _write_config(
            tmp_path, target_versions=["3.9 EA1 RHOAI RELEASE"]
        )
        # A missing/invisible key arrives as HTTP 404 from get_issue;
        # discover_issues converts it to an empty result so the loud
        # exit-5 path (and the artifact write) runs.
        with patch.object(qg, "get_issue", side_effect=_http_error(404)):
            with pytest.raises(SystemExit) as excinfo:
                with patch.object(qg, "verify_credentials"), patch.object(
                    qg, "write_artifacts"
                ) as mock_write:
                    qg.main(["--config", cfg, "--issue", "RHAISTRAT-9999"])
        assert excinfo.value.code == 5
        assert mock_write.called
        err = capsys.readouterr().err
        assert "ISSUE NOT FOUND" in err
        assert "RHAISTRAT-9999" in err

    def test_single_issue_server_error_propagates(self, jira_env, tmp_path):
        # Non-404 errors on the issue read are not swallowed.
        cfg = _write_config(
            tmp_path, target_versions=["3.9 EA1 RHOAI RELEASE"]
        )
        with patch.object(qg, "get_issue", side_effect=_http_error(500)):
            with pytest.raises(urllib.error.HTTPError):
                with patch.object(qg, "verify_credentials"):
                    qg.main(["--config", cfg, "--issue", "RHAISTRAT-9999"])

    def test_artifacts_still_written_before_loud_exit(self, jira_env, tmp_path):
        cfg = _write_config(
            tmp_path, target_versions=["3.9 EA1 RHOAI RELEASE"]
        )
        with patch.object(qg, "discover_issues", return_value=[]):
            with pytest.raises(SystemExit):
                with patch.object(qg, "verify_credentials"), patch.object(
                    qg, "write_artifacts"
                ) as mock_write:
                    qg.main(["--config", cfg])
        assert mock_write.called
