# release-planner-quality-gate

Quality Gate 1: Feature Definition of Ready for Planning. Validates that planning Features (and RHAISTRAT Initiatives) have FPDoR Phase 1 fields set before they are considered ready for release planning. Features that pass this gate have been scored, prioritized, and (for AI First Features) signed off — the minimum bar for entering release planning.

## What This Does

Given a set of Features discovered via JQL or specified individually, the pipeline:

1. **Discovers** candidate Features from Jira using configurable JQL filters
2. **Evaluates** each feature against a set of hard checks (RICE, priority, owners, AI First sign-off/rubric when applicable, components, docs impact, target version, child epics, and FPDoR description criteria)
3. **Auto-fixes** missing RICE scores using a Claude Code skill that researches the ticket and generates calibrated recommendations
4. **Re-evaluates** auto-fixed features to see if they now pass
5. **Labels** each feature with `rp-qg1-pass` or `rp-qg1-fail`
6. **Reports** results to `artifacts/run-data.json` and `artifacts/run-report.yaml`

### Discovery population

Discovery matches Org Pulse / Product Pages planning population:

`(RHAISTRAT ∧ Feature|Initiative) ∨ (AIPCC ∧ Feature)`

Further filters:

- **Target Version** (`cf[10855]`): cycles from `config/release-calendar.json` whose **code freeze is still in the future** (inclusive of today), expanded to Jira picklist names like `3.6 EA1 RHOAI RELEASE` / `RHAII` / `RHELAI` (not obsolete `rhoai-3.x`)
- **Status**: not Closed, Resolved, or Cancelled
- **Not** filtered on `strat-creator-human-sign-off` — that label is an AI First hard check, not a discovery prerequisite

Legacy Features (no `strat-creator-*` labels) stay in scope. Sign-off and rubric checks are marked N/A for them so they do not false-fail.

## Architecture

**"Agents analyze, scripts decide."**

The Python orchestrator (`quality_gate.py`) handles all deterministic logic: JQL queries, field validation, description scanning, verdict computation, label management, and Jira writes. The only Claude skill on the gate write path is RICE auto-fix:

- `/rice-score` — RICE recommendations when scores are missing
- Description FPDoR criteria — **deterministic scanner** (`description_signals.py`, Org Pulse port) plus label/field shortcuts. CI does **not** call Claude for description checks.

```text
quality_gate.py          →  JQL discovery, check evaluation, label management
  ├── checks/            →  Pluggable check framework (field_present, label_present, …)
  ├── description_signals.py → Org Pulse description-scanner port (no Claude)
  ├── rice_invoker.py    →  Spawns Claude headlessly, parses RICE output
  └── report.py          →  Generates JSON + YAML artifacts

/rice-score skill        →  Fetches ticket, reads attachments, applies RICE rubric
```

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency management
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (for RICE auto-generation)
- Jira API credentials:

```bash
export JIRA_SERVER=https://redhat.atlassian.net
export JIRA_USER=your-email@redhat.com
export JIRA_TOKEN=your-api-token
```

### Install

```bash
make install
```

### Run

```bash
# Single issue (testing)
make run-issue-dry ISSUE=RHAISTRAT-1745    # dry-run: generate RICE, no Jira writes
make run-issue ISSUE=RHAISTRAT-1745        # full run: write RICE + apply gate labels

# Batch (pipeline)
make run-dry                                # dry-run: evaluate all candidates
make run                                    # full run: evaluate, fix, label
```

`--dry-run` generates RICE recommendations locally but writes **nothing** to Jira — no fields, no comments, no labels.

## Checks

Gate 1 uses a **fixed 17-item FPDoR checklist** aligned with [Org Pulse `fpdor.js`](https://github.com/red-hat-data-services/rhai-org-pulse/blob/main/modules/releases/server/planning/fpdor.js) and the [Planning FPDoR Confluence page](https://redhat.atlassian.net/wiki/spaces/RHAI/pages/442958832). **17 items always; N/A counts as pass; only real failures block `rp-qg1-pass`.** Gate comments show `Score: X/17 (Y N/A, Z FAIL)` alongside the per-check table.

See [`docs/ORG-PULSE-ALIGNMENT.md`](docs/ORG-PULSE-ALIGNMENT.md) for the full item mapping and intentional differences (e.g. QG1’s separate `has_rubric_pass` vs Org Pulse’s Source RFE item).

Gate 1 hard checks:

| Check | Type | What It Validates | Auto-Fixable |
|-------|------|-------------------|--------------|
| `has_rice` | `field_present` | All 4 RICE fields are set (Reach, Impact, Confidence, Effort) | Yes — triggers Claude skill |
| `has_priority` | `field_present` | Priority field is set | No |
| `has_pm` | `field_present` | Product Manager (`customfield_10469`) is set | No |
| `has_delivery_owner` | `field_present` | Assignee (Delivery Owner) is set | No |
| `has_sign_off` | `label_present` | AI First only: has `strat-creator-human-sign-off` (N/A for Legacy) | No |
| `has_rubric_pass` | `label_present` | AI First only: has `strat-creator-rubric-pass` (N/A for Legacy) | No |
| `has_components` | `field_present` | At least one component assigned | No |
| `has_release_type` | `field_present` | Release Type (`customfield_10851`) is set | No |
| `has_docs_impact` | `docs_impact` | Product Documentation Required is set; if Yes, `Documentation` component assigned | No |
| `has_target_version` | `field_present` | Target Version (`customfield_10855`) is set | No |
| `has_child_epics` | `has_child_epics` | ≥1 child Epic with `parent` = this Feature in `RHOAIENG` / `RHAIENG` / `AIPCC` / `INFERENG` / `RHAI` / `RHELAI` | No |
| `has_requirements_clarity` | `description_criterion` | Problem/scope/requirements/use-case signals (or strat-creator label shortcuts) | No |
| `has_acceptance_criteria` | `description_criterion` | Acceptance/success criteria in description (or `strat-creator-rubric-pass`) | No |
| `has_risks_assumptions` | `description_criterion` | Risks/assumptions/constraints in description (or label shortcuts) | No |
| `has_architectural_alignment` | `description_criterion` | Architecture notes / “not required”, else N/A (or label shortcuts) | No |
| `has_uxd_description` | `description_criterion` | UXD component or explicit “N/A – no UX”; else N/A | No |
| `has_cross_team_deps` | `description_criterion` | ≥2 eng components, dependency language, or `epic-creator-auto-decomposed` | No |

**Verdict**: all applicable checks must pass → `rp-qg1-pass`. Any applicable failure → `rp-qg1-fail`. Checks marked N/A (`passed=True`, `not_applicable=True`) count toward the **17/17** score and do not block pass. The denominator is always 17 — N/A items are not removed from the list.

Description checks scan the Jira **description** field only (no attachments, no Claude). Failures are content fails (labels/comments are written). They are not infrastructure errors.

`has_child_epics` uses the same Jira parent/child model as RHAISTRAT engineering decomposition (`issuetype = Epic AND parent in (…)`, scoped to the six engineering projects). The orchestrator batch-fetches children before evaluation. The `epic-creator-auto-decomposed` label is **not** accepted as a substitute pass — QG1 verifies real child Epics (same structural preference as other non-label checks). If the child-Epic lookup fails with a transport or 5xx error, the run still writes artifacts with verdict `error` (excluded from the fail tally) and **skips gate label/comment writes** so an outage cannot mass-flip Features to `rp-qg1-fail`. Client HTTP 4xx errors (except rate-limit 429) propagate.

**AI First vs Legacy**: a Feature is treated as AI First when it has any `strat-creator-*` label. For those Features, `has_sign_off` and `has_rubric_pass` are required. Legacy Features skip those two checks (N/A) so missing AI First-only labels do not false-fail them. Discovery itself does not require human sign-off.

The check framework is extensible — new check types are registered via `@register_check("type_name")` in `scripts/checks/`.

## RICE Scoring

When a feature fails `has_rice`, the orchestrator invokes the `/rice-score` Claude skill headlessly. The skill:

1. Fetches the target ticket and all its fields
2. Downloads and reads strategy/review attachments (the richest evidence source)
3. Reads linked RFEs for customer context
4. Fetches 20 existing scored features as calibration anchors
5. Applies the RICE rubric and outputs a structured recommendation

### RICE Fields

| Dimension | Field ID | Valid Values | What It Measures |
|-----------|----------|--------------|------------------|
| Reach | `customfield_10862` | 1, 3, 5, 8, 13 | Market size — how many users/accounts benefit |
| Impact | `customfield_10836` | 1, 3, 5, 8, 13 | Per-user depth — how much it moves the needle |
| Confidence | `customfield_10838` | 50%, 75%, 100% | Evidence quality — how sure are we |
| Effort | `customfield_10637` | 1, 2, 3, 5, 8, 13 | Relative complexity — not person-months |
| RICE Score | `customfield_10864` | (auto-calculated) | (Reach × Impact × Confidence%) / Effort |

Effort is relative complexity, not calendar time. E=13 is a red flag that the feature should be split.

## Jira Labels

| Label | Meaning |
|-------|---------|
| `rp-qg1-pass` | Passed Gate 1 — all **17** FPDoR hard checks with fixed denominator (N/A items count as pass): fields (target version, release type, components, PM, delivery owner, priority, RICE), docs impact, six description criteria (requirements clarity, acceptance criteria, risks/assumptions, architectural alignment, UXD, cross-team deps), child epics, and AI First-only human sign-off + rubric when applicable. See `docs/ORG-PULSE-ALIGNMENT.md`. |
| `rp-qg1-fail` | Failed Gate 1 — missing one or more requirements |
| `rp-qg1-auto-rice` | RICE scores were auto-generated by the Claude skill |

Labels are applied atomically: adding `rp-qg1-pass` removes `rp-qg1-fail` (and vice versa). Existing unrelated labels are untouched.

Discovery does **not** exclude `rp-qg1-pass`. Prior passes stay in scope so tightened hard checks (for example FPDoR Phase 1 or Phase 2) revalidate existing labels. Optional opt-out labels may still be listed under `jql.skip_labels` (for example `rp-qg1-skip`).

On re-runs, the orchestrator evaluates every in-scope candidate, but **skips Jira comment/label writes** when the check result fingerprint (`QG1-FP`) on a **bot-authored** gate comment is unchanged and labels already match. The fingerprint includes a hash of the configured hard-check set, so adding or changing checks invalidates prior fingerprints and forces label/comment updates. Fingerprints from human/other-author comments are ignored for skip decisions. That prevents daily comment churn without freezing stale passes across criteria changes.

Batch runs write `artifacts/run-data.json` **before** applying labels/comments, isolate per-issue Jira write failures (one bad ticket cannot abort the rest), and fall back to posting a new gate comment when updating an existing one returns HTTP 400/403 (edit denied). Gate-comment updates only target comments authored by the authenticated bot — marker matches from humans/other bots are left alone and a new comment is added. Comment lists are fetched once per issue and filtered in memory.

## Updating the Release Calendar

Discovery only finds Target Versions whose **code freeze is still in the future**, resolved from `config/release-calendar.json`. When a new cycle (e.g. 3.7) is not in that file, discovery fail-closes to a never-match Target Version clause and every run reports `No issues found to evaluate.` — the job still exits 0. Re-sync the calendar whenever a new cycle starts planning or dates move:

```bash
uv run python scripts/update_release_calendar.py --dry-run   # preview changes
uv run python scripts/update_release_calendar.py             # write the file
```

The script reads the RHOAI planning-freeze / code-freeze / release milestones straight from [Product Pages](https://productpages.redhat.com) (API v7), the system of record for release schedules. Authentication, in order of precedence (the first that applies wins):

1. `PRODUCTPAGES_TOKEN` — a raw access token sent as a Bearer header.
2. `PRODUCTPAGES_CLIENT_ID` + `PRODUCTPAGES_CLIENT_SECRET` — an EmployeeIDP service account (client_credentials grant), for automation. Request one through IT's SSO service account form (Production environment).
3. **Kerberos ticket** (fallback when no credential env vars are set) — just `kinit` first; the script rides the Product Pages OIDC login flow with SPNEGO via `curl --negotiate`.

`--base-url` must be HTTPS on an approved Product Pages host (default allowlist: `productpages.redhat.com`, `pp.engineering.redhat.com`; extend via `PRODUCTPAGES_ALLOWED_HOSTS`) so credentials are never sent over cleartext or to an unexpected host.

Behavior notes:

- Events are keyed by `(version, event)`; releases fetched from Product Pages are added or updated, and everything else (including hand-adjusted historical rows) is left untouched. A date Product Pages does not carry never overwrites an existing value — and a previously reported milestone that vanishes from the fetch is called out in the output.
- Rows from a release page that belong to a different version (predecessor milestones copied onto later pages) are non-authoritative: they only fill gaps, never override.
- Dates in Product Pages move as schedules slip — the printed change list (and your git diff) is the review surface. Commit the regenerated file like any other config change.
- Draft and invalid schedule rows are ignored; non-ISO dates and malformed rows are skipped with a warning; freeze spans use the last day; release dates come only from the top-level `jira-sync` rows.
- The calendar is written atomically, so an interrupted run cannot truncate it. If nothing at all can be extracted, the script exits 2 (use `--allow-empty` to downgrade) rather than claiming the calendar is current.
- `generatedAt` / `sync.ranAt` record the last run that changed content; a provenance change (different releases or product line) also counts as a change so the sync block converges.
- The calendar refuses to sync a different `--product-line` over one already synced (events are keyed by `(version, event)` only — pass `--force-product-line` to override). `--releases rhai-3.7` syncs a single release.

The gate itself also fails loudly now: `quality_gate.py` exits 2 when a batch run finds no issues *and* the calendar resolves to no future code freeze, and a CI guard test requires the committed calendar to always contain a code freeze ≥ 30 days out — so an exhausted calendar turns red instead of silently evaluating nothing.

## Testing

```bash
make test              # All tests
make test-unit         # Checks + report only (fast, no server)
make test-integration  # Quality gate + label management (uses jira-emulator)
```

Integration tests use [jira-emulator](https://github.com/ederign/jira-emulator) — an in-memory Jira server that starts per-session and resets state per-test. No real Jira credentials needed for tests.

## Project Structure

```
scripts/
  quality_gate.py       # Main orchestrator — discover, evaluate, fix, label
  release_calendar.py   # Target Version resolution from release-calendar.json
  update_release_calendar.py  # Rebuild release-calendar.json from Product Pages
  rice_invoker.py       # Headless Claude /rice-score invocation + parsing
  description_signals.py # Org Pulse description-scanner port (gate path)
  report.py             # JSON + YAML artifact generation
  jira_utils.py         # Shared Jira API utilities (from strat-creator)
  checks/
    __init__.py          # Check framework — BaseCheck, registry, verdict logic
    hard_checks.py       # field_present, label_present, docs_impact, has_child_epics, description_criterion

tests/
  conftest.py            # jira-emulator fixtures
  test_checks.py         # Check framework + all check types
  test_description_signals.py  # Org Pulse scanner parity
  test_quality_gate.py   # JQL builder, config, evaluate, run-data
  test_fingerprint_skip.py  # QG1-FP stability / skip logic
  test_release_calendar.py  # Calendar Target Version helpers
  test_update_release_calendar.py  # Product Pages sync (parsing + merge)
  test_label_management.py  # Atomic label swap logic
  test_rice_invoker.py   # RICE structured output parsing
  test_report.py         # Report generation

config/
  pipeline-settings.yaml # JQL filters, check definitions, field IDs, labels
  release-calendar.json  # Code-freeze dates used for discovery Target Versions

.claude/skills/rice-score/
  SKILL.md               # RICE skill — workflow, output format
  references/
    rice-rubric.md       # Scoring scales and principles
    calibration-examples.md  # 25 scored features for anchoring
    jira-fields.md       # API reference and field IDs
```
