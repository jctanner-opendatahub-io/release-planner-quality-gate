"""Release Quality Gate 1: Feature Definition of Ready for Planning.

Main orchestrator: discovers issues, evaluates checks, manages labels.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
from datetime import datetime, timezone

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.checks import (
    instantiate_checks,
    compute_verdict,
    compute_fpdor_score,
    CheckResult,
)
import scripts.checks.hard_checks  # noqa: F401 — registers check types
from scripts.jira_utils import (
    require_env,
    search_issues,
    get_issue,
    add_labels,
    remove_labels,
    add_comment,
    markdown_to_adf,
    fetch_child_epics_by_parent,
    DEFAULT_ENGINEERING_PROJECTS,
)
from scripts.checks.hard_checks import (
    CHILD_EPICS_ATTR,
    preview_child_keys,
)
from scripts.rice_invoker import (
    generate_rice_scores,
    write_rice_to_jira,
)
from scripts.fpdor_severity import check_severity_label, sort_checks_for_display
from scripts.release_calendar import resolve_discovery_target_versions


CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "pipeline-settings.yaml"
)


def load_config(path=None):
    """Load pipeline settings from YAML."""
    path = path or CONFIG_PATH
    with open(path) as f:
        return yaml.safe_load(f)


def _scope_clause(scope: dict) -> str:
    """Build one project + issuetype scope clause."""
    project = scope["project"]
    types = scope.get("issuetypes") or ["Feature"]
    if len(types) == 1:
        type_clause = f'issuetype = {types[0]}'
    else:
        type_list = ", ".join(types)
        type_clause = f"issuetype in ({type_list})"
    return f"(project = {project} AND {type_clause})"


# Sentinel so empty calendar/TV resolution cannot scan the whole backlog.
_NO_TV_SENTINEL = "__qg1-no-discovery-target-versions__"


def _requires_target_version_clause(jql_cfg: dict) -> bool:
    """True when discovery must constrain Target Version (fail closed)."""
    if jql_cfg.get("target_versions"):
        return True
    return bool(jql_cfg.get("target_versions_from_calendar", True))


def build_jql(config, as_of=None):
    """Build discovery JQL from pipeline-settings config.

    Supports either legacy ``jql.project`` or multi-scope ``jql.scopes``.
    Target versions come from explicit ``target_versions`` or the release
    calendar (per-event future ``codeFreeze``).

    When calendar/explicit TV resolution is enabled but yields no names,
    JQL uses a never-match Target Version clause (fail closed) instead of
    omitting the filter and scanning the whole backlog.
    """
    jql_cfg = config["jql"]
    scopes = jql_cfg.get("scopes")
    if scopes:
        scope_jql = " OR ".join(_scope_clause(s) for s in scopes)
        clauses = [f"({scope_jql})"]
    else:
        clauses = [f'project = {jql_cfg["project"]}']

    for label in jql_cfg.get("required_labels") or []:
        clauses.append(f'labels = "{label}"')

    versions = resolve_discovery_target_versions(config, as_of=as_of)
    if versions:
        version_list = ", ".join(f'"{v}"' for v in versions)
        clauses.append(f"cf[10855] IN ({version_list})")
    elif _requires_target_version_clause(jql_cfg):
        clauses.append(f'cf[10855] = "{_NO_TV_SENTINEL}"')

    excluded = jql_cfg.get("excluded_statuses") or []
    if excluded:
        if len(excluded) == 1:
            clauses.append(f'status != "{excluded[0]}"')
        else:
            status_list = ", ".join(f'"{s}"' for s in excluded)
            clauses.append(f"status NOT IN ({status_list})")

    for label in jql_cfg.get("skip_labels") or []:
        clauses.append(f'labels != "{label}"')

    jql = " AND ".join(clauses)
    order = jql_cfg.get("order_by", "key ASC")
    return f"{jql} ORDER BY {order}"


def collect_required_fields(config):
    """Gather all Jira field names needed by the configured checks."""
    fields = {"key", "summary", "labels", "priority", "assignee"}
    for check_cfg in config.get("checks", {}).get("hard_checks", []):
        for f in check_cfg.get("fields", []):
            fields.add(f)
        for _ in check_cfg.get("labels", []):
            fields.add("labels")
        docs_field = check_cfg.get("docs_required_field")
        if docs_field:
            fields.add(docs_field)
            fields.add("components")
        if check_cfg.get("type") == "description_criterion":
            fields.add("labels")
            fields.add("components")
            fields.add("description")
    rice = config.get("rice_fields", {})
    for field_id in rice.values():
        fields.add(field_id)
    return sorted(fields)


def discover_issues(config, server, user, token, issue_key=None):
    """Discover issues to evaluate — single key or JQL batch."""
    fields = collect_required_fields(config)
    if issue_key:
        issue = get_issue(server, user, token, issue_key, fields=fields)
        return [issue] if issue else []
    jql = build_jql(config)
    return search_issues(server, user, token, jql, fields=fields)


def _child_epics_check_config(config):
    """Return the has_child_epics hard-check config, or None."""
    for check_cfg in config.get("checks", {}).get("hard_checks", []):
        if check_cfg.get("type") == "has_child_epics":
            return check_cfg
    return None


def enrich_issues_with_child_epics(issues, config, server, user, token):
    """Attach child Epic summaries for has_child_epics evaluation.

    On success, sets ``issue["_child_epics"]`` to a list (possibly empty).
    On transport / server lookup failure, sets ``_child_epics`` to ``None``
    for every issue and returns False so callers can suppress gate
    label/comment writes (infra errors must not flip Features to
    ``rp-qg1-fail``). Client HTTP errors (4xx except 429) propagate.

    Returns True when enrichment succeeded or was a no-op (check not
    configured / empty issue list).
    """
    check_cfg = _child_epics_check_config(config)
    if not check_cfg or not issues:
        return True
    projects = check_cfg.get("engineering_projects") or list(
        DEFAULT_ENGINEERING_PROJECTS)
    keys = [issue["key"] for issue in issues]
    try:
        by_parent = fetch_child_epics_by_parent(
            server, user, token, keys, projects=projects)
    except urllib.error.HTTPError as exc:
        # HTTPError subclasses URLError — handle first.
        # 4xx (except rate-limit) are client/config bugs and must propagate.
        if exc.code < 500 and exc.code != 429:
            raise
        print(f"Child Epic lookup failed: {exc}", file=sys.stderr)
        for issue in issues:
            issue[CHILD_EPICS_ATTR] = None
        return False
    except urllib.error.URLError as exc:
        # Network / DNS / connection failures after retries.
        print(f"Child Epic lookup failed: {exc}", file=sys.stderr)
        for issue in issues:
            issue[CHILD_EPICS_ATTR] = None
        return False
    for issue in issues:
        issue[CHILD_EPICS_ATTR] = by_parent.get(issue["key"], [])
    return True


def should_suppress_gate_write(issue, config):
    """True when required enrichment failed (infra) — leave Jira labels alone.

    Empty child-epic list is a content failure and must still write.
    Missing / None enrichment data is an infrastructure gap.
    """
    if _child_epics_check_config(config) and issue.get(CHILD_EPICS_ATTR) is None:
        return True
    return False


def evaluate_issue(issue, checks):
    """Run all checks against a single issue, return list of CheckResult."""
    return [check.evaluate(issue) for check in checks]


def apply_verdict_label(server, user, token, issue_key, current_labels,
                        verdict, label_config):
    """Apply pass/fail label based on verdict. Atomic swap.

    Infrastructure ``error`` verdicts leave labels unchanged.
    """
    if verdict == "error":
        return

    pass_label = label_config["gate_pass"]
    fail_label = label_config["gate_fail"]

    new_label = pass_label if verdict == "pass" else fail_label
    old_label = fail_label if verdict == "pass" else pass_label

    has_new = new_label in current_labels
    has_old = old_label in current_labels

    if has_new and not has_old:
        return  # already correct

    labels_to_add = [] if has_new else [new_label]
    labels_to_remove = [old_label] if has_old else []

    if labels_to_add:
        add_labels(server, user, token, issue_key, labels_to_add)
    if labels_to_remove:
        remove_labels(server, user, token, issue_key, labels_to_remove)


CHECK_LABELS = {
    "has_rice": "RICE Score",
    "has_priority": "Priority",
    "has_pm": "Product Manager",
    "has_delivery_owner": "Delivery Owner",
    "has_sign_off": "Human Sign-off",
    "has_rubric_pass": "Strategy Rubric Pass",
    "has_components": "Components",
    "has_release_type": "Release Type",
    "has_docs_impact": "Docs Impact",
    "has_docs_required": "Product Docs Required",  # legacy alias
    "has_target_version": "Target Version",
    "has_child_epics": "Child Epics",
    "has_requirements_clarity": "Requirements Clarity",
    "has_acceptance_criteria": "Acceptance Criteria",
    "has_risks_assumptions": "Risks & Assumptions",
    "has_architectural_alignment": "Architectural Alignment",
    "has_uxd_description": "UXD (description)",
    "has_cross_team_deps": "Cross-team Dependencies",
}

FIELD_FRIENDLY_NAMES = {
    "customfield_10862": "Reach",
    "customfield_10836": "Impact",
    "customfield_10838": "Confidence",
    "customfield_10637": "Effort",
    "customfield_10851": "Release Type",
    "customfield_10665": "Product Documentation Required",
    "customfield_10855": "Target Version",
    "customfield_10469": "Product Manager",
    "priority": "Priority",
    "assignee": "Assignee",
    "components": "Components",
}


def _extract_field_detail(issue, check_name):
    """Extract a human-readable value for a check from the issue fields."""
    f = issue.get("fields", {})
    if check_name == "has_rice":
        r = f.get("customfield_10862", "?")
        i = f.get("customfield_10836", "?")
        c_obj = f.get("customfield_10838")
        c = c_obj.get("value", "?") if isinstance(c_obj, dict) else "?"
        e = f.get("customfield_10637", "?")
        return f"R={r}, I={i}, C={c}, E={e}"
    if check_name == "has_priority":
        p = f.get("priority")
        return p.get("name", "?") if isinstance(p, dict) else str(p or "?")
    if check_name == "has_pm":
        pm = f.get("customfield_10469")
        return pm.get("displayName", "?") if isinstance(pm, dict) else str(pm or "?")
    if check_name == "has_delivery_owner":
        a = f.get("assignee")
        return a.get("displayName", "?") if isinstance(a, dict) else str(a or "?")
    if check_name == "has_sign_off":
        return "strat-creator-human-sign-off label present"
    if check_name == "has_rubric_pass":
        return "strat-creator-rubric-pass label present"
    if check_name == "has_components":
        comps = f.get("components", [])
        return ", ".join(c.get("name", "?") for c in comps) if comps else "?"
    if check_name == "has_release_type":
        rt = f.get("customfield_10851")
        return rt.get("value", "?") if isinstance(rt, dict) else str(rt or "?")
    if check_name in ("has_docs_impact", "has_docs_required"):
        dr = f.get("customfield_10665")
        docs = dr.get("value", "?") if isinstance(dr, dict) else str(dr or "?")
        comps = f.get("components", [])
        names = ", ".join(c.get("name", "?") for c in comps) if comps else "none"
        return f"{docs}; components: {names}"
    if check_name == "has_target_version":
        tv = f.get("customfield_10855", [])
        if isinstance(tv, list):
            return ", ".join(v.get("name", "?") for v in tv) if tv else "?"
        if isinstance(tv, dict):
            return tv.get("name", "?")
        return str(tv or "?")
    if check_name == "has_child_epics":
        children = issue.get(CHILD_EPICS_ATTR) or []
        if not children:
            return "none"
        return preview_child_keys(children)
    return "?"


def _friendly_fail_details(details):
    """Replace custom field IDs with human-readable names in failure details."""
    result = details
    for field_id, name in FIELD_FRIENDLY_NAMES.items():
        result = result.replace(field_id, name)
    return result


def compute_checks_version(hard_checks_config):
    """Stable short hash of the configured hard-check set.

    Changing check names, types, or parameters invalidates stored
    fingerprints so prior pass/fail labels are revalidated.
    """
    payload = json.dumps(
        hard_checks_config or [], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def _format_fpdor_score_line(check_results, verdict):
    """Build Org Pulse–aligned score summary for gate comments."""
    score = compute_fpdor_score(check_results)
    if score["error_count"]:
        return (
            f"**Score: {score['passed_count']}/{score['total_count']}** "
            f"({score['na_count']} N/A, {score['fail_count']} FAIL, "
            f"{score['error_count']} ERROR)"
        )
    return (
        f"**Score: {score['passed_count']}/{score['total_count']}** "
        f"({score['na_count']} N/A, {score['fail_count']} FAIL)"
    )


def build_gate_comment(issue, check_results, verdict, label_config,
                       checks_version=""):
    """Build a deterministic gate result comment in markdown."""
    status = {"pass": "PASS", "fail": "FAIL", "error": "ERROR"}.get(
        verdict, "FAIL")
    if verdict == "pass":
        label = label_config["gate_pass"]
    elif verdict == "fail":
        label = label_config["gate_fail"]
    else:
        label = "unchanged (infrastructure error)"

    lines = [
        f"**Release Quality Gate 1: Feature Definition of Ready for Planning — {status}**",
        "",
        _format_fpdor_score_line(check_results, verdict),
        "",
    ]

    if verdict == "pass":
        lines.append("All hard checks passed for this feature.")
    elif verdict == "error":
        lines.append("Evaluation incomplete due to infrastructure errors.")
    else:
        failed = [
            r for r in check_results
            if not r.passed and not r.not_applicable and not r.infra_error
        ]
        lines.append(f"{len(failed)} check(s) failed.")
    lines.append("")

    lines.append("Checks ordered by importance (critical first).")
    lines.append("")

    issue_labels = issue.get("fields", {}).get("labels", [])
    has_auto_rice = "rp-qg1-auto-rice" in issue_labels

    lines.append("| Check | Importance | Status | Details |")
    lines.append("|-------|------------|--------|---------|")
    for r in sort_checks_for_display(check_results):
        check_label = CHECK_LABELS.get(r.name, r.name)
        if r.name == "has_rice" and has_auto_rice:
            check_label = "RICE Score (Auto-generated)"
        if r.not_applicable:
            status_icon = "N/A"
            detail = r.details
        elif r.infra_error:
            status_icon = "ERROR"
            detail = r.details
        elif r.passed:
            status_icon = "PASS"
            detail = _extract_field_detail(issue, r.name)
            if detail == "?" and r.details:
                detail = r.details
        else:
            status_icon = "FAIL"
            detail = _friendly_fail_details(r.details)
        importance = check_severity_label(r.name)
        lines.append(
            f"| {check_label} | {importance} | {status_icon} | {detail} |")

    lines.append("")
    lines.append(f"Label applied: {label}")
    lines.append(
        f"QG1-FP: {compute_result_fingerprint(check_results, verdict, checks_version)}"
    )

    if verdict == "fail":
        lines.append("")
        lines.append(
            "To resolve: fix the failing checks above in Jira. "
            "The next pipeline run will re-evaluate automatically and "
            "update this comment and labels only if the result changes or "
            "the gate labels don't match yet."
        )

    return "\n".join(lines)


GATE_COMMENT_MARKER = "Release Quality Gate 1: Feature Definition of Ready for Planning"
FINGERPRINT_RE = re.compile(r"QG1-FP:\s*([a-f0-9]{16,64})", re.IGNORECASE)


def compute_result_fingerprint(check_results, verdict, checks_version=""):
    """Stable hash of checks version + verdict + per-check outcomes.

    checks_version must change when hard-check criteria change so prior
    gate comments no longer match and stale pass/fail labels are rewritten.
    """
    data = {
        "checks_version": checks_version or "",
        "verdict": verdict,
        "checks": [
            {
                "name": r.name,
                "status": (
                    "na" if r.not_applicable
                    else "error" if r.infra_error
                    else "pass" if r.passed
                    else "fail"
                ),
                # Verdict only — do not hash free-text evidence/details.
                "detail": (
                    "na" if r.not_applicable
                    else "error" if r.infra_error
                    else "ok" if r.passed
                    else "fail"
                ),
            }
            for r in check_results
        ],
    }
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def extract_fingerprint(comment_text):
    """Pull QG1-FP hash from a gate comment body, if present.

    Returns None when missing, malformed, or ambiguous (multiple QG1-FP
    tokens) so should_skip_jira_write() forces a rewrite.
    """
    matches = FINGERPRINT_RE.findall(comment_text or "")
    if len(matches) != 1:
        return None
    return matches[0].lower()


def labels_match_verdict(current_labels, verdict, label_config):
    """True when pass/fail labels already match the verdict (no swap needed).

    ``error`` never matches — callers must not treat infra errors as fail.
    """
    if verdict == "error":
        return False
    pass_label = label_config["gate_pass"]
    fail_label = label_config["gate_fail"]
    labels = current_labels or []
    if verdict == "pass":
        return pass_label in labels and fail_label not in labels
    return fail_label in labels and pass_label not in labels


_current_account_id = None


def _get_current_account_id(server, user, token):
    """Resolve the authenticated Jira accountId (cached for the process)."""
    global _current_account_id
    if _current_account_id is not None:
        return _current_account_id
    from scripts.jira_utils import api_call_with_retry
    me = api_call_with_retry(server, "/myself", user, token)
    _current_account_id = (me or {}).get("accountId")
    return _current_account_id


def _comment_authored_by_current_user(comment, server, user, token):
    """True when the comment author is the authenticated API user."""
    author = comment.get("author") or {}
    account_id = _get_current_account_id(server, user, token)
    if account_id and author.get("accountId") == account_id:
        return True
    # Fallback for environments where /myself is unavailable in tests.
    email = (author.get("emailAddress") or "").lower()
    return bool(email and email == (user or "").lower())


_UNSET = object()


def _find_gate_comment(server, user, token, issue_key, owned_by_self=False,
                       comments=None):
    """Find an existing gate comment (latest match).

    When owned_by_self is True, only comments authored by the authenticated
    user are considered — required because Jira rejects edits to other users'
    comments even when they contain the gate marker.

    Pass comments= to reuse a single get_comments() fetch across filters.

    Returns (comment_id, markdown_text) or (None, None).
    """
    from scripts.jira_utils import get_comments, adf_to_markdown
    if comments is None:
        comments = get_comments(server, user, token, issue_key)
    matches = []
    for comment in comments:
        body = comment.get("body", {})
        text = adf_to_markdown(body) if isinstance(body, dict) else str(body)
        if GATE_COMMENT_MARKER not in text:
            continue
        if owned_by_self and not _comment_authored_by_current_user(
                comment, server, user, token):
            continue
        matches.append((comment.get("id"), text))
    if not matches:
        return None, None
    return matches[-1]


def _update_comment(server, user, token, issue_key, comment_id, body_adf):
    """PUT to update an existing comment."""
    from scripts.jira_utils import api_call_with_retry
    path = f"/issue/{issue_key}/comment/{comment_id}"
    return api_call_with_retry(server, path, user, token,
                               body={"body": body_adf}, method="PUT")


def post_gate_comment(server, user, token, issue_key, comment_md,
                      existing_id=_UNSET):
    """Post or update the gate result comment on Jira.

    Only updates comments authored by the authenticated user. Marker-matching
    comments from humans/other bots are left alone and a new comment is added.
    If an owned-comment update still fails with 400/403, fall back to add.

    Pass existing_id=None when the caller already confirmed there is no
    bot-authored gate comment (avoids a redundant comment-list fetch).
    """
    comment_adf = markdown_to_adf(comment_md)
    if existing_id is _UNSET:
        existing_id, _ = _find_gate_comment(
            server, user, token, issue_key, owned_by_self=True)
    if existing_id:
        try:
            _update_comment(server, user, token, issue_key, existing_id,
                            comment_adf)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 403):
                raise
            print(
                f"  {issue_key}: cannot edit comment {existing_id} "
                f"(HTTP {e.code}); posting a new comment instead",
                file=sys.stderr,
            )
            add_comment(server, user, token, issue_key, comment_adf)
    else:
        add_comment(server, user, token, issue_key, comment_adf)


def write_issue_gate_result(server, user, token, issue, results, label_config,
                            checks_version=""):
    """Apply labels + gate comment for one issue.

    Returns "skipped" when fingerprint/labels are unchanged or when the
    verdict is an infrastructure ``error`` (labels must stay unchanged),
    else "written".
    """
    from scripts.jira_utils import get_comments

    key = issue["key"]
    verdict = compute_verdict(results)
    if verdict == "error":
        return "skipped"
    current_labels = issue.get("fields", {}).get("labels", [])
    new_fp = compute_result_fingerprint(results, verdict, checks_version)
    # One comment-list fetch; only trust fingerprints from bot-authored comments.
    comments = get_comments(server, user, token, key)
    own_id, own_text = _find_gate_comment(
        server, user, token, key, owned_by_self=True, comments=comments)
    existing_fp = extract_fingerprint(own_text)
    if should_skip_jira_write(
            existing_fp, new_fp, current_labels, verdict, label_config):
        return "skipped"
    apply_verdict_label(
        server, user, token, key, current_labels, verdict, label_config)
    comment_md = build_gate_comment(
        issue, results, verdict, label_config, checks_version=checks_version)
    post_gate_comment(
        server, user, token, key, comment_md, existing_id=own_id)
    return "written"


def should_skip_jira_write(existing_fingerprint, new_fingerprint,
                           current_labels, verdict, label_config):
    """Skip comment/label writes when result fingerprint and labels are unchanged."""
    if not existing_fingerprint:
        return False
    if existing_fingerprint.lower() != new_fingerprint.lower():
        return False
    return labels_match_verdict(current_labels, verdict, label_config)


def build_run_data(results_by_issue, config, dry_run, mode, issue_key=None):
    """Build the run-data.json structure."""
    issues_data = []
    pass_count = 0
    fail_count = 0
    error_count = 0

    for key, results in results_by_issue.items():
        verdict = compute_verdict(results)
        if verdict == "pass":
            pass_count += 1
        elif verdict == "error":
            error_count += 1
        else:
            fail_count += 1
        issues_data.append({
            "key": key,
            "verdict": verdict,
            "fpdor": compute_fpdor_score(results),
            "checks": {
                r.name: {
                    "passed": r.passed,
                    "details": r.details,
                    "auto_fixable": r.auto_fixable,
                    "infra_error": r.infra_error,
                    "not_applicable": r.not_applicable,
                }
                for r in results
            },
        })

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "mode": mode,
        "summary": {
            "total": len(results_by_issue),
            "pass": pass_count,
            "fail": fail_count,
            "error": error_count,
        },
        "issues": issues_data,
    }
    if issue_key:
        data["issue_key"] = issue_key
    return data


def write_artifacts(run_data):
    """Write run-data.json to artifacts/."""
    art_dir = os.path.join(os.path.dirname(__file__), "..", "artifacts")
    os.makedirs(art_dir, exist_ok=True)
    path = os.path.join(art_dir, "run-data.json")
    with open(path, "w") as f:
        json.dump(run_data, f, indent=2)
    return path


def print_summary(results_by_issue):
    """Print a human-readable summary table."""
    print(f"\n{'='*70}")
    print(f"{'Key':<20} {'Verdict':<10} {'Details'}")
    print(f"{'-'*70}")
    for key, results in results_by_issue.items():
        verdict = compute_verdict(results)
        failed = [
            r for r in results
            if not r.passed and not r.not_applicable
        ]
        if failed:
            details = "; ".join(f"{r.name}: {r.details}" for r in failed)
        elif any(r.not_applicable for r in results):
            details = "all applicable checks passed"
        else:
            details = "all checks passed"
        status = {"pass": "PASS", "fail": "FAIL", "error": "ERROR"}.get(
            verdict, verdict.upper())
        print(f"{key:<20} {status:<10} {details}")

    total = len(results_by_issue)
    passed = sum(1 for r in results_by_issue.values()
                 if compute_verdict(r) == "pass")
    errored = sum(1 for r in results_by_issue.values()
                  if compute_verdict(r) == "error")
    failed = total - passed - errored
    print(f"{'-'*70}")
    print(
        f"Total: {total}  |  Pass: {passed}  |  Fail: {failed}"
        f"  |  Error: {errored}"
    )
    print(f"{'='*70}\n")


def _discovery_fail_closed(config) -> bool:
    """True when discovery Target Version resolution is empty (fail-closed).

    In that state every batch run evaluates zero issues because ``build_jql``
    emits the never-match sentinel — the calendar is exhausted and needs a
    sync (``scripts/update_release_calendar.py``).
    """
    jql_cfg = config.get("jql") or {}
    if jql_cfg.get("target_versions"):
        return False
    if not jql_cfg.get("target_versions_from_calendar", True):
        return False
    try:
        return not resolve_discovery_target_versions(config)
    except OSError:
        return True  # calendar file missing/unreadable — also fail-closed


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Release Quality Gate 1: Feature Definition of Ready for Planning"
    )
    parser.add_argument(
        "--issue", metavar="KEY",
        help="Evaluate a single issue (e.g., RHAISTRAT-1745)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate RICE recommendations but write NOTHING to Jira")
    parser.add_argument(
        "--config", metavar="PATH",
        help="Path to pipeline-settings.yaml (default: config/pipeline-settings.yaml)")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    server, user, token = require_env()

    mode = "single" if args.issue else "batch"
    print(f"Release Quality Gate 1 — mode={mode}, dry_run={args.dry_run}")

    issues = discover_issues(config, server, user, token,
                             issue_key=args.issue)
    if not issues:
        if not args.issue and _discovery_fail_closed(config):
            print("WARNING: no issues found AND the release calendar resolves "
                  "to no future code freeze — discovery fail-closed to the "
                  "never-match sentinel. Update config/release-calendar.json "
                  "(run scripts/update_release_calendar.py).", file=sys.stderr)
            sys.exit(2)
        print("No issues found to evaluate.")
        run_data = build_run_data({}, config, args.dry_run, mode, args.issue)
        write_artifacts(run_data)
        return

    print(f"Found {len(issues)} issue(s) to evaluate.")
    enrich_issues_with_child_epics(issues, config, server, user, token)

    checks = instantiate_checks(config["checks"]["hard_checks"])
    fields = collect_required_fields(config)
    results_by_issue = {}

    for issue in issues:
        key = issue["key"]
        results = evaluate_issue(issue, checks)
        results_by_issue[key] = results

    # Auto-fix: generate RICE for issues missing it (capped per batch).
    needs_rice = [
        key for key, results in results_by_issue.items()
        if any(r.name == "has_rice" and not r.passed and r.auto_fixable
               for r in results)
    ]
    rice_cfg = config.get("rice_scorer") or {}
    max_auto = rice_cfg.get("max_auto_fix")
    if max_auto is not None and len(needs_rice) > int(max_auto):
        print(
            f"\nAuto-RICE capped at {max_auto} of {len(needs_rice)} "
            f"missing-RICE issue(s); remainder stay fail until a later run."
        )
        needs_rice = needs_rice[: int(max_auto)]

    rice_generated = {}
    if needs_rice:
        timeout = rice_cfg.get("timeout_seconds", 300)
        print(f"\nGenerating RICE for {len(needs_rice)} issue(s)...")
        rice_result = generate_rice_scores(needs_rice, timeout=timeout)

        rice_written = []
        for rec in rice_result.succeeded:
            rice_generated[rec.ticket] = rec
            if args.dry_run:
                continue
            try:
                print(f"  Writing RICE to Jira for {rec.ticket}...")
                write_rice_to_jira(rec, server, user, token)
                rice_written.append(rec)
            except Exception as exc:
                # Isolate per-ticket failures so one RICE write cannot abort
                # the run before artifacts / gate labels are produced.
                print(
                    f"  {rec.ticket}: RICE write failed: {exc}",
                    file=sys.stderr,
                )

        # Re-fetch and re-evaluate issues that got RICE written.
        # Description signals are deterministic from the description field;
        # do not re-scan after RICE (RICE does not change description).
        if rice_written:
            print(f"\nRe-evaluating {len(rice_written)} RICE'd issues...")
            refetched = []
            for rec in rice_written:
                issue = get_issue(server, user, token, rec.ticket,
                                 fields=fields)
                if not issue:
                    print(
                        f"  {rec.ticket}: refetch failed after RICE write",
                        file=sys.stderr,
                    )
                    continue
                refetched.append(issue)
            if refetched:
                enrich_issues_with_child_epics(
                    refetched, config, server, user, token)
                for issue in refetched:
                    results_by_issue[issue["key"]] = evaluate_issue(
                        issue, checks)
                    for i, orig in enumerate(issues):
                        if orig["key"] == issue["key"]:
                            issues[i] = issue
                            break

    print_summary(results_by_issue)

    # Emit artifacts before gate label/comment writes so CI still gets
    # run-data.json if a later write crashes the process. RICE writes above
    # are also isolated per ticket for the same reason. Child-Epic lookup
    # failures are isolated too (enrich sets _child_epics=None) and must
    # not flip Jira labels — see should_suppress_gate_write().
    run_data = build_run_data(
        results_by_issue, config, args.dry_run, mode, args.issue)
    artifact_path = write_artifacts(run_data)
    print(f"Artifacts written to {artifact_path}")

    # Apply verdict labels and post gate comment (full run only).
    # Skip Jira writes when the result fingerprint is unchanged and labels
    # already match — avoids daily comment churn on sticky fails.
    # Isolate per-issue failures so one HTTP error cannot abort the batch.
    if not args.dry_run:
        label_config = config["labels"]
        checks_version = compute_checks_version(
            config.get("checks", {}).get("hard_checks", []))
        for issue in issues:
            key = issue["key"]
            results = results_by_issue[key]
            verdict = compute_verdict(results)
            suppress = should_suppress_gate_write(issue, config)
            if suppress or verdict == "error":
                reason = (
                    "child Epic enrichment failed" if suppress
                    else "infrastructure error"
                )
                print(
                    f"  {key}: skip write ({reason}; labels unchanged)",
                    file=sys.stderr,
                )
                continue
            try:
                outcome = write_issue_gate_result(
                    server, user, token, issue, results, label_config,
                    checks_version=checks_version)
                if outcome == "skipped":
                    print(f"  {key}: {verdict.upper()}"
                          f" (unchanged, skip write)")
                else:
                    print(f"  {key}: {verdict.upper()}"
                          f" → label + comment applied")
            except Exception as exc:
                print(f"  {key}: Jira write failed: {exc}", file=sys.stderr)
    else:
        for key, results in results_by_issue.items():
            verdict = compute_verdict(results)
            rice_note = ""
            if key in rice_generated:
                rec = rice_generated[key]
                rice_note = (f" | RICE generated: R={rec.reach} I={rec.impact}"
                             f" C={rec.confidence}% E={rec.effort}"
                             f" → {rec.expected_rice}")
            print(f"  {key}: {verdict.upper()}"
                  f" (dry-run, no Jira writes){rice_note}")


if __name__ == "__main__":
    main()
