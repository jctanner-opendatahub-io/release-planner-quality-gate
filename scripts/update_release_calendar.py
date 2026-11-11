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

3. Kerberos ticket (default; no setup beyond ``kinit``)
   A Red Hat laptop with a valid ticket can ride the web login flow with
   SPNEGO. This path shells out to ``curl --negotiate`` (any curl built with
   GSS-API support) for the two redirects that need SPNEGO, then reuses the
   resulting session cookie for the plain REST calls.

What the script does to the calendar:

- Events are keyed by ``(version, event)``. Dates for fetched releases are
  added or updated; events for releases that were not fetched (e.g. ones no
  longer listed in Product Pages) are left untouched, so historical and
  hand-adjusted rows survive.
- A date that Product Pages does not carry (``null``) never overwrites an
  existing non-null value; the script warns instead.
- Draft and invalid schedule rows are ignored.
- ``generatedAt`` is refreshed and a ``sync`` block records the run. The
  original ``note`` field is preserved verbatim.

Exit status is 0 even when changes were made; review the printed diff (and
the git diff) before committing.
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

# "<version> <event> <PRODUCT LINE> Planning Freeze|Code Freeze"
# (\s+ everywhere: some rows carry double spaces, e.g. "3.5 GA  RHOAI Code Freeze")
_FREEZE_RE = re.compile(
    r"^(?P<version>\d+(?:\.\d+)+)\s+(?P<event>EA\d+|GA)\s+"
    r"(?P<product>RHOAI|RHAII|RHELAI|AIPCC)\s+"
    r"(?P<kind>Planning|Code)\s+Freeze\s*$"
)
# Top-level "<version> <event> <PRODUCT LINE> RELEASE" rows (jira-sync flag)
_RELEASE_RE = re.compile(
    r"^(?P<version>\d+(?:\.\d+)+)\s+(?P<event>EA\d+|GA)\s+"
    r"(?P<product>RHOAI|RHAII|RHELAI|AIPCC)\s+RELEASE\s*$"
)
_RELEASE_SHORTNAME_RE = re.compile(r"^\d+\.\d+$")


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
    if not body:
        raise ProductPagesError(f"empty response from {url}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        # Unauthenticated calls can come back as an HTML login page.
        raise ProductPagesError(
            f"non-JSON response from {url} (not authenticated?)") from exc


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Treat redirects as errors so callers can inspect them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _form_post(url: str, form: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise ProductPagesError(
            f"token request failed: HTTP {exc.code} for {url}") from exc


def _curl(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "60", *args],
            capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise ProductPagesError(
            "curl not found — needed for the Kerberos (SPNEGO) login flow; "
            "install curl or set PRODUCTPAGES_TOKEN") from exc
    except subprocess.CalledProcessError as exc:
        raise ProductPagesError(f"curl exited {exc.returncode}: {exc.stderr}") from exc
    return result.stdout


def _parse_cookie_jar(path: str, host: str) -> dict[str, str]:
    """Read a Netscape-format curl cookie jar into a name→value dict."""
    cookies: dict[str, str] = {}
    host_tail = host.removeprefix("www.")
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
            if not (host_tail == domain or host_tail.endswith("." + domain)):
                continue
            cookies[parts[5]] = parts[6]
    return cookies


def authenticate(base_url: str) -> Auth:
    """Pick an authentication method and establish a session."""
    token = os.environ.get("PRODUCTPAGES_TOKEN")
    if token:
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


def _authenticate_kerberos(base_url: str) -> Auth:
    """Run the Product Pages web login flow with SPNEGO via curl."""
    host = urllib.parse.urlsplit(base_url).netloc
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".cookies") as jar:
        jar_path = jar.name
        # Step 1: start the OIDC flow; collect the session cookie and the
        # redirect to auth.redhat.com (Keycloak, EmployeeIDP).
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".headers") as headers:
            _curl(["-c", jar_path, "-D", headers.name, "-o", "/dev/null",
                   f"{base_url}/oidc/authenticate"])
            headers.seek(0)
            location = ""
            for line in headers.read().splitlines():
                if line.lower().startswith("location:"):
                    location = line.split(":", 1)[1].strip()
        if not location:
            raise ProductPagesError(
                "no login redirect from /oidc/authenticate — is Product Pages up?")
        # Step 2: SPNEGO against Keycloak with the Kerberos ticket, then
        # follow the callback chain; the session cookie becomes authenticated.
        status = _curl(["--negotiate", "-u", ":", "-b", jar_path, "-c", jar_path,
                        "-L", "-o", "/dev/null", "-w", "%{http_code}", location])
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
    url = urllib.parse.urljoin(base_url + API_ROOT, path.lstrip("/"))
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _http_json(url, auth)


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
    if not isinstance(body, list):
        raise ProductPagesError(f"unexpected releases response for {product}")
    shortnames = []
    for release in body:
        shortname = release.get("shortname") or ""
        version = shortname.removeprefix(f"{product}-")
        if _RELEASE_SHORTNAME_RE.fullmatch(version):
            shortnames.append((_version_key(version), shortname))
    return [shortname for _, shortname in sorted(shortnames)]


def fetch_schedule_tasks(base_url: str, auth: Auth, release: str) -> list[dict]:
    body = api_get(base_url, auth, f"releases/{release}/schedule-tasks/")
    if not isinstance(body, list):
        raise ProductPagesError(f"unexpected schedule-tasks response for {release}")
    return body


# ─── Task parsing ────────────────────────────────────────────────────────────

def _version_key(version: str) -> tuple[int, ...]:
    parts = version.split(".")
    if not all(part.isdigit() for part in parts):
        return (0,)
    return tuple(int(part) for part in parts)


def _task_date(task: dict) -> str | None:
    """The effective date of a milestone row: last day of a span, if any."""
    return task.get("date_finish") or task.get("date_start") or None


def _is_draft(task: dict) -> bool:
    return bool(task.get("draft")) or "draft" in (task.get("flags") or [])


def extract_events(tasks: list[dict], product_line: str) -> dict[tuple[str, str], dict]:
    """Mine schedule rows for (version, event) → freeze/release dates.

    Only rows naming ``product_line`` (e.g. RHOAI) are considered; RHAII and
    RHELAI ship on their own trains with their own dates. Release dates come
    from the top-level ``jira-sync`` rows that mirror the Jira Target Version
    picklists.
    """
    events: dict[tuple[str, str], dict] = {}
    candidates: dict[tuple[str, str, str], list[tuple[int, bool, str]]] = {}
    for task in tasks:
        if task.get("invalid") or _is_draft(task):
            continue
        name = " ".join(str(task.get("name") or "").split())
        if not name:
            continue

        freeze = _FREEZE_RE.match(name)
        release = _RELEASE_RE.match(name)
        if freeze:
            if freeze.group("product") != product_line:
                continue
            kind = f"{freeze.group('kind').lower()}Freeze"
            version, event = freeze.group("version"), freeze.group("event")
        elif release:
            if release.group("product") != product_line:
                continue
            kind = "releaseDate"
            version, event = release.group("version"), release.group("event")
        else:
            continue

        task_date = _task_date(task)
        if task_date is None:
            continue
        level = int(task.get("level") or 0)
        jira_sync = "jira-sync" in (task.get("flags") or [])
        candidates.setdefault((version, event, kind), []).append(
            (level, jira_sync, task_date))

    for (version, event, kind), rows in candidates.items():
        # Prefer the shallowest row (top-level roll-ups are what the release
        # page publishes), then jira-synced rows, then the latest date.
        rows.sort(key=lambda row: (row[0], not row[1]))
        top = [row for row in rows
               if row[0] == rows[0][0] and row[1] == rows[0][1]]
        level, jira_sync, task_date = max(top, key=lambda row: row[2])
        if len(rows) > 1 and len({row[2] for row in rows}) > 1:
            others = [row for row in rows[1:] if row[2] != task_date]
            print(f"  note: {version} {event} {kind}: using {task_date} "
                  f"(level {level}); other rows disagree: "
                  + ", ".join(f"{row[2]} (level {row[0]})" for row in others),
                  file=sys.stderr)
        events.setdefault((version, event), {})[kind] = task_date
    return events


# ─── Calendar merge ──────────────────────────────────────────────────────────

def default_calendar_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "config",
                        "release-calendar.json")


def _event_sort_key(event: dict) -> tuple[tuple[int, ...], str]:
    return (_version_key(event["version"]), event["event"])


def merge_calendar(calendar: dict, new_events: dict[tuple[str, str], dict],
                   releases: list[str], product_line: str) -> tuple[dict, list[str]]:
    """Merge fetched events into a calendar dict. Returns (calendar, changes)."""
    changes: list[str] = []
    existing: dict[tuple[str, str], dict] = {}
    for event in calendar.get("events") or []:
        existing[(event["version"], event["event"])] = event

    for (version, event), dates in sorted(new_events.items()):
        if not any(dates.values()):
            print(f"  note: {version} {event}: no usable dates, skipped",
                  file=sys.stderr)
            continue
        current = existing.get((version, event))
        if current is None:
            entry = {"version": version, "event": event}
            entry.update(dates)
            calendar.setdefault("events", []).append(entry)
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

    calendar["events"].sort(key=_event_sort_key)
    calendar["generatedAt"] = date.today().isoformat()
    calendar["sync"] = {
        "tool": "scripts/update_release_calendar.py",
        "source": "productpages.redhat.com (Product Pages API v7)",
        "ranAt": date.today().isoformat(),
        "productLine": product_line,
        "releases": releases,
    }
    return calendar, changes


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
                        help=f"product train to track dates for "
                             f"(default: {DEFAULT_PRODUCT_LINE})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"Product Pages base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the pending changes without writing the file")
    args = parser.parse_args(argv)

    auth = authenticate(args.base_url)
    user = whoami(args.base_url, auth)
    print(f"Authenticated to Product Pages as {user} ({auth.label}).")

    if args.releases:
        releases = [name.strip() for name in args.releases.split(",") if name.strip()]
    else:
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
        new_events.update(found)
    if drafts_dropped:
        print(f"Ignored {drafts_dropped} draft/invalid task(s) across releases.")

    with open(args.calendar, encoding="utf-8") as f:
        calendar = json.load(f)
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
    with open(args.calendar, "w", encoding="utf-8") as f:
        json.dump(calendar, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote {len(changes)} change(s) to {args.calendar}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProductPagesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
