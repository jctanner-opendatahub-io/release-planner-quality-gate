"""Rebuild/update config/release-calendar.json from Red Hat Product Pages.

Product Pages (productpages.redhat.com) is the system of record for release
schedules. This script fetches the milestone tasks for the RHAI releases it
finds there (or the ones you name) and merges the planning-freeze /
code-freeze / release dates into the release calendar used by QG1 discovery.

Run it by hand when a new cycle needs to be added or dates have moved::

    python scripts/update_release_calendar.py --dry-run   # preview changes
    python scripts/update_release_calendar.py             # write the file

Authentication — the first option that applies is used:

1. ``PRODUCTPAGES_TOKEN`` environment variable
   A raw access token, sent as ``Authorization: Bearer``.

2. ``PRODUCTPAGES_CLIENT_ID`` + ``PRODUCTPAGES_CLIENT_SECRET``
   An EmployeeIDP service account (client_credentials grant), for future
   automation. Request one through IT's SSO service account form; ask for
   the Production environment.

3. Kerberos ticket (fallback; no setup beyond ``kinit``)
   A Red Hat laptop with a valid ticket can ride the web login flow with
   SPNEGO. This path shells out to ``curl --negotiate`` (any curl built with
   GSS-API support) for the two redirects that need SPNEGO, then reuses the
   resulting session cookie for the plain REST calls.

What the script does to the calendar:

- Events are keyed by ``(version, event)``. Dates for fetched releases are
  added or updated; events for releases that were not fetched (e.g. ones no
  longer listed in Product Pages) are left untouched, so historical and
  hand-adjusted rows survive.
- A date that Product Pages does not carry never overwrites an existing
  non-null value; the script warns instead (including when a previously
  reported milestone disappears from the fetch).
- Rows from a release page whose version does not match that release are
  treated as non-authoritative: they only fill gaps, never override.
- Draft and invalid schedule rows are ignored; non-ISO dates and malformed
  rows are skipped with a warning; freeze spans use the last day.
- Release dates come only from the top-level ``jira-sync`` rows that mirror
  the Jira Target Version picklists.
- ``--base-url`` must be HTTPS on an approved Product Pages host (default
  allowlist: ``productpages.redhat.com``, ``pp.engineering.redhat.com``;
  extend via ``PRODUCTPAGES_ALLOWED_HOSTS``) so credentials are never sent
  over cleartext or to an unexpected host.
- The calendar is written atomically (temp file + ``os.replace``), so an
  interrupted run cannot truncate it.
- ``generatedAt`` / ``sync.ranAt`` record the last run that *changed
  content*; a change to the sync provenance itself (different releases or
  product line) also counts as a change so provenance converges.
- The calendar refuses to sync a different ``--product-line`` over one
  already synced (use ``--force-product-line`` to override), because the
  calendar schema keys events by ``(version, event)`` only.

Exit status: 0 on success (with or without changes), 2 when nothing at all
could be extracted from Product Pages (``--allow-empty`` downgrades that to
a warning), 1 on errors.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

DEFAULT_BASE_URL = "https://productpages.redhat.com"
API_ROOT = "/api/v7/"
TOKEN_URL = "https://auth.redhat.com/auth/realms/EmployeeIDP/protocol/openid-connect/token"
DEFAULT_PRODUCT = "rhai"
DEFAULT_PRODUCT_LINE = "RHOAI"
PRODUCT_LINES = ("RHOAI", "RHAII", "RHELAI", "AIPCC")
DEFAULT_ALLOWED_HOSTS = ("productpages.redhat.com", "pp.engineering.redhat.com")
LOGIN_HOSTS = ("auth.redhat.com",)
RELEASE_SHORTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Missing/malformed rows rank as the deepest (least preferred) level.
_DEEPEST_LEVEL = 1_000_000

# "<version> <event> <PRODUCT LINE> Planning Freeze|Code Freeze"
# (\s+ everywhere: some rows carry double spaces, e.g. "3.5 GA  RHOAI Code Freeze";
#  IGNORECASE: kind words are capitalized consistently today, but be tolerant)
_FREEZE_RE = re.compile(
    r"^(?P<version>\d+(?:\.\d+)+)\s+(?P<event>EA\d+|GA)\s+"
    r"(?P<product>RHOAI|RHAII|RHELAI|AIPCC)\s+"
    r"(?P<kind>Planning|Code)\s+Freeze\s*$",
    re.IGNORECASE,
)
# Top-level "<version> <event> <PRODUCT LINE> RELEASE" rows (jira-sync flag).
# Only jira-sync rows are accepted: they mirror the Jira Target Version
# picklists and are the canonical release dates.
_RELEASE_RE = re.compile(
    r"^(?P<version>\d+(?:\.\d+)+)\s+(?P<event>EA\d+|GA)\s+"
    r"(?P<product>RHOAI|RHAII|RHELAI|AIPCC)\s+RELEASE\s*$",
    re.IGNORECASE,
)
_BARE_VERSION_RE = re.compile(r"^\d+\.\d+$")


class ProductPagesError(Exception):
    """Raised on unrecoverable Product Pages API errors."""


# ─── HTTP layer ──────────────────────────────────────────────────────────────

class Auth:
    """Headers/cookies for API calls. Holds either a bearer token or a cookie."""

    def __init__(self, bearer: str | None = None, cookies: dict[str, str] | None = None):
        self.bearer = bearer
        self.cookies = cookies or {}

    @property
    def label(self) -> str:
        if self.bearer:
            return "bearer token"
        if self.cookies:
            return "kerberos session cookie"
        return "anonymous"

    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
        elif self.cookies:
            headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items())
        return headers


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Treat redirects as errors so callers can inspect them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _http_json(url: str, auth: Auth, data: bytes | None = None,
               headers: dict[str, str] | None = None) -> object:
    """GET (or POST when data is given) and decode a JSON response."""
    all_headers = auth.headers()
    all_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=all_headers)
    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        with opener.open(request, timeout=60) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise ProductPagesError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise ProductPagesError(f"network error for {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProductPagesError(f"timeout for {url}") from exc
    except ValueError as exc:
        # e.g. http.client rejecting a header value; the message can embed
        # the credential — never let it reach the user.
        raise ProductPagesError(
            "invalid request header value (credential not echoed)") from exc
    if not body:
        raise ProductPagesError(f"empty response from {url}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        # Unauthenticated calls can come back as an HTML login page.
        raise ProductPagesError(
            f"non-JSON response from {url} (not authenticated?)") from exc


def _form_post(url: str, form: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise ProductPagesError(
            f"token request failed: HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise ProductPagesError(
            f"token request network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProductPagesError("token request timed out") from exc
    except ValueError as exc:
        raise ProductPagesError(
            "invalid request header value (credential not echoed)") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProductPagesError(
            "non-JSON token response (SSO down or redirecting?)") from exc


def _curl(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["curl", "-sS", "--max-time", "60",
             "--proto", "=https", "--proto-redir", "=https", *args],
            capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise ProductPagesError(
            "curl not found — needed for the Kerberos (SPNEGO) login flow; "
            "install curl or set PRODUCTPAGES_TOKEN") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        raise ProductPagesError(
            f"curl exited {exc.returncode}: {detail}") from exc
    return result.stdout


def _parse_cookie_jar(path: str, host: str) -> dict[str, str]:
    """Read a Netscape-format curl cookie jar into a name→value dict."""
    cookies: dict[str, str] = {}
    with open(path, encoding="utf-8") as jar:
        for line in jar:
            # curl writes HttpOnly cookies as "#HttpOnly_<domain>\t...".
            is_httponly = line.startswith("#HttpOnly_")
            if line.startswith("#") and not is_httponly:
                continue
            if is_httponly:
                line = line[len("#HttpOnly_"):]
            if not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 7:
                continue
            domain = parts[0].lstrip(".")
            if not (host == domain or host.endswith("." + domain)):
                continue
            cookies[parts[5]] = parts[6]
    return cookies


def _allowed_hosts() -> set[str]:
    raw = os.environ.get(
        "PRODUCTPAGES_ALLOWED_HOSTS", ",".join(DEFAULT_ALLOWED_HOSTS))
    return {host.strip() for host in raw.split(",") if host.strip()}


def _validate_base_url(base_url: str) -> str:
    """Require an HTTPS URL on an approved Product Pages host; return hostname."""
    origin = urllib.parse.urlsplit(base_url)
    if origin.scheme != "https" or not origin.hostname:
        raise ProductPagesError(
            f"--base-url must be an HTTPS URL, got {base_url!r}")
    if origin.path not in ("", "/") or origin.query or origin.fragment:
        raise ProductPagesError(
            f"--base-url must not carry a path/query/fragment: {base_url!r}")
    allowed = _allowed_hosts()
    if origin.hostname not in allowed:
        raise ProductPagesError(
            f"--base-url host {origin.hostname!r} is not an approved Product "
            f"Pages host (approved: {', '.join(sorted(allowed))}; extend via "
            "PRODUCTPAGES_ALLOWED_HOSTS)")
    return origin.hostname


def authenticate(base_url: str) -> Auth:
    """Pick an authentication method and establish a session."""
    token = (os.environ.get("PRODUCTPAGES_TOKEN") or "").strip()
    if token:
        if re.search(r"\s", token):
            raise ProductPagesError(
                "PRODUCTPAGES_TOKEN contains internal whitespace — refusing "
                "to use a malformed credential")
        return Auth(bearer=token)

    client_id = os.environ.get("PRODUCTPAGES_CLIENT_ID")
    client_secret = os.environ.get("PRODUCTPAGES_CLIENT_SECRET")
    if client_id and client_secret:
        body = _form_post(TOKEN_URL, {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        })
        if "access_token" not in body:
            raise ProductPagesError("token response missing access_token")
        return Auth(bearer=body["access_token"])

    return _authenticate_kerberos(base_url)


def _authenticate_kerberos(base_url: str, host: str | None = None) -> Auth:
    """Run the Product Pages web login flow with SPNEGO via curl."""
    host = host or _validate_base_url(base_url)
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".cookies") as jar:
        jar_path = jar.name
        # Step 1: start the OIDC flow; collect the session cookie and the
        # redirect to auth.redhat.com (Keycloak, EmployeeIDP).
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".headers") as headers:
            _curl(["-c", jar_path, "-D", headers.name, "-o", "/dev/null",
                   "--url", f"{base_url}/oidc/authenticate"])
            headers.seek(0)
            location = ""
            for line in headers.read().splitlines():
                if line.lower().startswith("location:"):
                    location = line.split(":", 1)[1].strip()
        if not location:
            raise ProductPagesError(
                "no login redirect from /oidc/authenticate — is Product Pages up?")
        # The redirect must stay on HTTPS and on an expected host, and must
        # look like a URL (a leading "-" would be parsed by curl as an option).
        target = urllib.parse.urlsplit(location)
        if target.scheme != "https" or target.hostname not in (host, *LOGIN_HOSTS):
            raise ProductPagesError(
                f"login redirected to unexpected URL host "
                f"{target.hostname!r} — refusing to follow")
        # Step 2: SPNEGO against Keycloak with the Kerberos ticket, then
        # follow the callback chain; the session cookie becomes authenticated.
        status = _curl(["--negotiate", "-u", ":", "-b", jar_path, "-c", jar_path,
                        "-L", "-o", "/dev/null", "-w", "%{http_code}",
                        "--url", location])
        if status != "200":
            raise ProductPagesError(
                f"Kerberos login failed (final status {status}). "
                "Check `klist` for a valid ticket (kinit) and curl GSS support.")
        cookies = _parse_cookie_jar(jar_path, host)
        if not cookies:
            raise ProductPagesError("no session cookie after Kerberos login")
        return Auth(cookies=cookies)


# ─── API calls ───────────────────────────────────────────────────────────────

def api_get(base_url: str, auth: Auth, path: str, params: dict[str, str] | None = None):
    """GET an API path (or an absolute pagination cursor URL) as JSON."""
    if urllib.parse.urlsplit(path).scheme:
        url = path
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(base_url).netloc:
            raise ProductPagesError(f"refusing off-origin pagination URL: {url}")
    else:
        url = urllib.parse.urljoin(base_url + API_ROOT, path.lstrip("/"))
        if params:
            url += "?" + urllib.parse.urlencode(params)
    return _http_json(url, auth)


def _drf_items(base_url: str, auth: Auth, body: object, what: str) -> list:
    """Return list items from a bare array or a DRF-style paginated envelope."""
    items: list = []
    while True:
        if isinstance(body, list):
            return items + body
        if isinstance(body, dict) and isinstance(body.get("results"), list):
            items.extend(body["results"])
            next_url = body.get("next")
            if not next_url:
                return items
            body = api_get(base_url, auth, next_url)
            continue
        raise ProductPagesError(f"unexpected {what} response shape")


def whoami(base_url: str, auth: Auth) -> str:
    body = api_get(base_url, auth, "whoami/")
    if not isinstance(body, dict) or "username" not in body:
        raise ProductPagesError("unexpected whoami response — auth may have failed")
    return body["username"]


def fetch_releases(base_url: str, auth: Auth, product: str) -> list[str]:
    """Versioned release shortnames for a product (e.g. rhai-3.7).

    Excludes special releases such as rhai-Z-Stream: the calendar tracks
    EA/GA cycle events, not z-stream patch dates.
    """
    body = api_get(base_url, auth, "releases/", {
        "product__shortname": product,
        "fields": "shortname,name",
    })
    entries = _drf_items(base_url, auth, body, f"releases for {product}")
    shortnames = []
    for release in entries:
        if not isinstance(release, dict):
            continue
        shortname = release.get("shortname") or ""
        version = shortname.removeprefix(f"{product}-")
        if _BARE_VERSION_RE.fullmatch(version):
            shortnames.append((_version_key(version), shortname))
    return [shortname for _, shortname in sorted(shortnames)]


def fetch_schedule_tasks(base_url: str, auth: Auth, release: str) -> list[dict]:
    path = f"releases/{urllib.parse.quote(release, safe='')}/schedule-tasks/"
    body = api_get(base_url, auth, path)
    entries = _drf_items(base_url, auth, body, f"schedule-tasks for {release}")
    return [entry for entry in entries if isinstance(entry, dict)]


# ─── Task parsing ────────────────────────────────────────────────────────────

def _version_key(version: str) -> tuple[int, ...]:
    parts = version.split(".")
    if not all(part.isdigit() for part in parts):
        return (0,)
    return tuple(int(part) for part in parts)


def _task_date(task: dict) -> str | None:
    """Effective date of a milestone row: last day of a span, ISO-normalized.

    Non-ISO values are skipped with a warning — the calendar consumer drops
    unparsable codeFreeze values silently, which would re-darken discovery.
    """
    raw = task.get("date_finish") or task.get("date_start") or None
    if raw is None:
        return None
    try:
        return date.fromisoformat(str(raw)[:10]).isoformat()
    except ValueError:
        print(f"  note: skipping non-ISO date {raw!r} in task "
              f"{str(task.get('name'))[:60]!r}", file=sys.stderr)
        return None


def _task_level(task: dict) -> int:
    """Schedule level; missing or malformed ranks as deepest (least preferred)."""
    try:
        return int(task.get("level"))
    except (TypeError, ValueError):
        return _DEEPEST_LEVEL


def _is_draft(task: dict) -> bool:
    return bool(task.get("draft")) or "draft" in (task.get("flags") or [])


def extract_events(tasks: list[dict], product_line: str) -> dict[tuple[str, str], dict]:
    """Mine schedule rows for (version, event) → freeze/release dates.

    Only rows naming ``product_line`` (e.g. RHOAI) are considered; RHAII and
    RHELAI ship on their own trains with their own dates. Release dates come
    only from top-level ``jira-sync`` rows that mirror the Jira Target
    Version picklists.
    """
    events: dict[tuple[str, str], dict] = {}
    candidates: dict[tuple[str, str, str], list[tuple[int, bool, str]]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            print(f"  note: skipping non-dict schedule row: {str(task)[:60]!r}",
                  file=sys.stderr)
            continue
        if task.get("invalid") or _is_draft(task):
            continue
        name = " ".join(str(task.get("name") or "").split())
        if not name:
            continue

        flags = set(task.get("flags") or [])
        freeze = _FREEZE_RE.match(name)
        release = _RELEASE_RE.match(name)
        if freeze:
            if freeze.group("product").upper() != product_line:
                continue
            kind = f"{freeze.group('kind').lower()}Freeze"
            version, event = freeze.group("version"), freeze.group("event").upper()
        elif release:
            if release.group("product").upper() != product_line:
                continue
            if "jira-sync" not in flags:
                continue  # launch/roadmap rows are not canonical release dates
            kind = "releaseDate"
            version, event = release.group("version"), release.group("event").upper()
        else:
            continue

        task_date = _task_date(task)
        if task_date is None:
            continue
        level = _task_level(task)
        jira_sync = "jira-sync" in flags
        candidates.setdefault((version, event, kind), []).append(
            (level, jira_sync, task_date))

    for (version, event, kind), rows in candidates.items():
        # Prefer the shallowest row (top-level roll-ups are what the release
        # page publishes), then jira-synced rows, then the latest date.
        rows.sort(key=lambda row: (row[0], not row[1]))
        top = [row for row in rows
               if row[0] == rows[0][0] and row[1] == rows[0][1]]
        level, jira_sync, task_date = max(top, key=lambda row: row[2])
        others = [row for row in rows if row[2] != task_date]
        if others:
            detail = ", ".join(f"{row[2]} (level {row[0]})" for row in others)
            print(f"  note: {version} {event} {kind}: using {task_date} "
                  f"(level {level}); other rows disagree: {detail[:200]}",
                  file=sys.stderr)
        events.setdefault((version, event), {})[kind] = task_date
    return events


def _merge_release_events(new_events: dict[tuple[str, str], dict],
                          found: dict[tuple[str, str], dict],
                          release: str, product: str) -> None:
    """Fold one release page's events into the accumulating set.

    Rows from the release whose shortname matches the version are
    authoritative; rows for other versions that show up on a later page
    (predecessor/dependency milestones) only fill gaps and never override.
    """
    for key, dates in found.items():
        version, event = key
        current = new_events.get(key)
        if current is None:
            new_events[key] = dict(dates)
            continue
        authoritative = release == f"{product}-{version}"
        for field, value in dates.items():
            if field not in current:
                current[field] = value
            elif current[field] != value and authoritative:
                print(f"  note: {release} overrides {version} {event} {field}: "
                      f"{current[field]} -> {value}", file=sys.stderr)
                current[field] = value
            elif current[field] != value:
                print(f"  note: {release} page disagrees on {version} {event} "
                      f"{field} ({value}); keeping {current[field]}", file=sys.stderr)


# ─── Calendar merge ──────────────────────────────────────────────────────────

def default_calendar_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "config",
                        "release-calendar.json")


def _event_sort_key(event: dict) -> tuple[tuple[int, ...], int, int]:
    """Order events by version, then EA1 < EA2 < … < GA (EA10 after EA9)."""
    label = str(event.get("event") or "")
    ea = re.fullmatch(r"EA(\d+)", label)
    if ea:
        rank, sub = 0, int(ea.group(1))
    elif label == "GA":
        rank, sub = 1, 0
    else:
        rank, sub = 2, 0
    return (_version_key(str(event.get("version") or "")), rank, sub)


def merge_calendar(calendar: dict, new_events: dict[tuple[str, str], dict],
                   releases: list[str], product_line: str) -> tuple[dict, list[str]]:
    """Merge fetched events into a calendar dict. Returns (calendar, changes)."""
    changes: list[str] = []
    existing: dict[tuple[str, str], dict] = {}
    kept_rows: list[dict] = []
    for event in calendar.get("events") or []:
        key = (event.get("version"), event.get("event"))
        if not all(isinstance(part, str) for part in key):
            print(f"  note: skipping malformed calendar row: {str(event)[:60]!r}",
                  file=sys.stderr)
            continue
        if key in existing:
            print(f"  note: duplicate calendar row for {key[0]} {key[1]}; "
                  "keeping the first", file=sys.stderr)
            continue
        existing[key] = event
        kept_rows.append(event)

    for (version, event), dates in sorted(new_events.items()):
        if not any(dates.values()):
            print(f"  note: {version} {event}: no usable dates, skipped",
                  file=sys.stderr)
            continue
        current = existing.get((version, event))
        if current is None:
            entry = {"version": version, "event": event}
            entry.update(dates)
            kept_rows.append(entry)
            existing[(version, event)] = entry
            changes.append(f"added {version} {event}: {dates}")
            continue
        for field, new_value in dates.items():
            old_value = current.get(field)
            if new_value == old_value:
                continue
            if new_value is None:
                print(f"  note: {version} {event}: Product Pages has no "
                      f"{field}; keeping existing {old_value!r}", file=sys.stderr)
                continue
            current[field] = new_value
            changes.append(
                f"updated {version} {event} {field}: {old_value} -> {new_value}")
        # A previously reported milestone that vanished from the fetch is
        # worth surfacing even though the value is preserved.
        for field, old_value in current.items():
            if field in ("version", "event") or field in dates:
                continue
            if old_value is not None:
                print(f"  note: {version} {event}: Product Pages did not report "
                      f"{field}; keeping existing {old_value!r}", file=sys.stderr)

    calendar["events"] = kept_rows
    calendar["events"].sort(key=_event_sort_key)
    today = date.today().isoformat()
    calendar["generatedAt"] = today
    new_sync = {
        "tool": "scripts/update_release_calendar.py",
        "source": "productpages.redhat.com (Product Pages API v7)",
        "ranAt": today,
        "productLine": product_line,
        "releases": list(releases),
    }
    old_sync = calendar.get("sync")
    if not isinstance(old_sync, dict):
        calendar["note"] = (
            "Dates for synced events come from Product Pages "
            f"(scripts/update_release_calendar.py, {today}); see the sync block. "
            "Historical note follows.\n\n" + str(calendar.get("note") or ""))
        changes.append("recorded Product Pages provenance (sync block + note)")
    elif (old_sync.get("releases") != new_sync["releases"]
            or old_sync.get("productLine") != new_sync["productLine"]):
        changes.append(
            f"updated sync provenance: releases {old_sync.get('releases')} -> "
            f"{new_sync['releases']}, productLine {old_sync.get('productLine')} "
            f"-> {new_sync['productLine']}")
    calendar["sync"] = new_sync
    return calendar, changes


def _write_calendar(path: str, calendar: dict) -> None:
    """Write the calendar atomically: temp sibling file + os.replace."""
    payload = json.dumps(calendar, indent=2, ensure_ascii=False) + "\n"
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".release-calendar-",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ─── Entry point ─────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild/update config/release-calendar.json from "
                    "Red Hat Product Pages.")
    parser.add_argument("--calendar", metavar="PATH",
                        default=default_calendar_path(),
                        help="calendar file to update "
                             "(default: config/release-calendar.json)")
    parser.add_argument("--product", default=DEFAULT_PRODUCT,
                        help=f"Product Pages product shortname "
                             f"(default: {DEFAULT_PRODUCT})")
    parser.add_argument("--releases", metavar="LIST",
                        help="comma-separated release shortnames to sync "
                             "(default: every versioned release of --product)")
    parser.add_argument("--product-line", default=DEFAULT_PRODUCT_LINE,
                        choices=PRODUCT_LINES,
                        help=f"product train to track dates for "
                             f"(default: {DEFAULT_PRODUCT_LINE})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"Product Pages base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the pending changes without writing the file")
    parser.add_argument("--allow-empty", action="store_true",
                        help="exit 0 instead of 2 when nothing could be "
                             "extracted from Product Pages")
    parser.add_argument("--force-product-line", action="store_true",
                        help="allow overwriting a calendar synced for a "
                             "different product line")
    args = parser.parse_args(argv)

    host = _validate_base_url(args.base_url)

    if args.releases:
        releases = [name.strip() for name in args.releases.split(",") if name.strip()]
        for name in releases:
            if not RELEASE_SHORTNAME_RE.fullmatch(name):
                parser.error(f"invalid release shortname: {name!r}")
    else:
        releases = None

    try:
        with open(args.calendar, encoding="utf-8") as f:
            calendar = json.load(f)
    except OSError as exc:
        raise ProductPagesError(f"cannot read calendar {args.calendar}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProductPagesError(
            f"calendar {args.calendar} is not valid JSON: {exc}") from exc

    synced_line = (calendar.get("sync") or {}).get("productLine")
    if (isinstance(synced_line, str) and synced_line != args.product_line
            and not args.force_product_line):
        raise ProductPagesError(
            f"calendar was last synced for product line {synced_line!r}; its "
            f"events are keyed by (version, event) only, so syncing "
            f"{args.product_line!r} would overwrite {synced_line!r}-sourced "
            f"dates. Use --product-line {synced_line}, or pass "
            "--force-product-line to proceed anyway")

    auth = authenticate(args.base_url)
    user = whoami(args.base_url, auth)
    print(f"Authenticated to Product Pages as {user} ({auth.label}).")

    if releases is None:
        releases = fetch_releases(args.base_url, auth, args.product)
    if not releases:
        raise ProductPagesError(
            f"no versioned releases found for product {args.product!r}")
    print(f"Syncing {len(releases)} release(s): {', '.join(releases)}")

    new_events: dict[tuple[str, str], dict] = {}
    drafts_dropped = 0
    for release in releases:
        tasks = fetch_schedule_tasks(args.base_url, auth, release)
        usable = [task for task in tasks
                  if not task.get("invalid") and not _is_draft(task)]
        drafts_dropped += len(tasks) - len(usable)
        found = extract_events(tasks, args.product_line)
        print(f"  {release}: {len(tasks)} tasks, {len(usable)} usable "
              f"(non-draft, non-invalid), {len(found)} event(s)")
        _merge_release_events(new_events, found, release, args.product)
    if drafts_dropped:
        print(f"Ignored {drafts_dropped} draft/invalid task(s) across releases.")

    if not new_events:
        print("WARNING: no matching schedule events were extracted from any "
              "release — Product Pages row names or --product-line may have "
              "changed. The calendar was NOT updated.", file=sys.stderr)
        if not args.allow_empty:
            return 2

    calendar, changes = merge_calendar(
        calendar, new_events, releases, args.product_line)

    if not changes:
        print("Calendar is already up to date.")
        return 0
    for change in changes:
        print(f"  {change}")
    if args.dry_run:
        print(f"Dry run: {len(changes)} change(s) pending, nothing written.")
        return 0
    _write_calendar(args.calendar, calendar)
    print(f"Wrote {len(changes)} change(s) to {args.calendar}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProductPagesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
