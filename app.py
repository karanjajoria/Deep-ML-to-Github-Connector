"""Deep-ML -> GitHub sync service (hardened).

Open the page, enter the problem title + your code, and it commits
solutions/<slug>.py to your repo. Re-committing the same problem updates the
same file (no copies) and the README index entry. Problems can be marked
solved/unsolved, and "manual entries" log a problem without any code.
"""
import base64
import hashlib
import json
import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse

import requests
from flask import Flask, abort, g, jsonify, redirect, render_template_string, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("deepml-sync")

# --------------------------------------------------------------------------
# Configuration (all secrets come from environment variables, never code)
# --------------------------------------------------------------------------
def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _required(name):
    value = _env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _https_url(name, default):
    value = _env(name, default)
    p = urlparse(value)
    return value if p.scheme == "https" and p.netloc else default


TOKEN = _required("GITHUB_TOKEN")
REPO = _required("GITHUB_REPO")
APP_KEY = _required("APP_KEY")
BRANCH = _env("GITHUB_BRANCH", "main")

if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", REPO):
    raise RuntimeError("GITHUB_REPO must look like 'owner/repo'")
if not re.fullmatch(r"[A-Za-z0-9._/-]+", BRANCH):
    raise RuntimeError("GITHUB_BRANCH contains unsupported characters")
QUEUE_BRANCH = _env("QUEUE_BRANCH", "deepml-queue")
if not re.fullmatch(r"[A-Za-z0-9._/-]+", QUEUE_BRANCH) or QUEUE_BRANCH == BRANCH:
    raise RuntimeError("QUEUE_BRANCH must be a valid branch name different from GITHUB_BRANCH")
if len(APP_KEY) < 16:
    raise RuntimeError("APP_KEY must be at least 16 characters (use a long random string)")

APP_KEY_HASH = hashlib.sha256(APP_KEY.encode()).digest()

GITHUB_URL = _https_url("GITHUB_URL", "https://github.com/karanjajoria")
REPO_URL = _https_url("REPO_URL", f"https://github.com/{REPO}")
LINKEDIN_URL = _https_url("LINKEDIN_URL", "https://www.linkedin.com/")
CREDLY_URL = _https_url("CREDLY_URL", "https://www.credly.com/")

MAX_CODE_CHARS = 200_000
MAX_TITLE_CHARS = 100
MAX_NOTE_CHARS = 100
DIFFICULTIES = {"", "easy", "medium", "hard"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024  # reject oversized bodies early
# Render terminates TLS in front of us; trust exactly one proxy hop.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# --------------------------------------------------------------------------
# GitHub API helpers
# --------------------------------------------------------------------------
BASE = f"https://api.github.com/repos/{REPO}/contents"
SESSION = requests.Session()
SESSION.headers.update(
    {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "deepml-sync",
    }
)


class GitHubError(Exception):
    def __init__(self, status):
        super().__init__(f"GitHub status {status}")
        self.status = status


def _gh(method, path, **kwargs):
    try:
        return SESSION.request(method, f"{BASE}/{path}", timeout=(5, 20), **kwargs)
    except requests.RequestException as exc:
        log.warning("GitHub network error: %s", type(exc).__name__)
        raise GitHubError(0)


def get_file(path, branch=None):
    r = _gh("GET", path, params={"ref": branch or BRANCH})
    if r.status_code == 404:
        return None, None
    if not r.ok:
        log.warning("GitHub GET %s -> %s", path, r.status_code)
        raise GitHubError(r.status_code)
    data = r.json()
    if not isinstance(data, dict) or "content" not in data:
        raise GitHubError(500)
    return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]


def put_file(path, content, message, sha=None, branch=None):
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode(),
        "branch": branch or BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = _gh("PUT", path, json=body)
    if not r.ok:
        log.warning("GitHub PUT %s -> %s", path, r.status_code)
        raise GitHubError(r.status_code)
    return r.json().get("commit", {}).get("html_url", "")


def delete_file(path, sha, message, branch=None):
    r = _gh("DELETE", path, json={"message": message, "sha": sha, "branch": branch or BRANCH})
    if not r.ok:
        log.warning("GitHub DELETE %s -> %s", path, r.status_code)
        raise GitHubError(r.status_code)


def _git(method, path, **kwargs):
    try:
        return SESSION.request(method, f"https://api.github.com/repos/{REPO}/git/{path}", timeout=(5, 20), **kwargs)
    except requests.RequestException as exc:
        log.warning("GitHub network error: %s", type(exc).__name__)
        raise GitHubError(0)


def list_solutions():
    """Slugs of every solutions/*.py file."""
    r = _gh("GET", "solutions", params={"ref": BRANCH})
    if r.status_code == 404:
        return []
    if not r.ok:
        raise GitHubError(r.status_code)
    out = []
    for item in r.json():
        m = re.fullmatch(r"([a-z0-9_]{1,100})\.py", item.get("name", ""))
        if m and item.get("type") == "file":
            out.append(m.group(1))
    return sorted(out)


# --------------------------------------------------------------------------
# Input validation (everything user-supplied is validated server-side)
# --------------------------------------------------------------------------
TITLE_RE = re.compile(r"^[\w\s\-.,:&'+/()#?!]+$", re.UNICODE)
URL_RE = re.compile(r"^https://(www\.)?deep-ml\.com(/[^\s()<>\"'`\\]*)?$")
SLUG_RE = re.compile(r"^[a-z0-9_]{1,100}$")


def slugify(title):
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def clean_title(value):
    if not isinstance(value, str):
        raise ValueError("Problem title is required.")
    title = re.sub(r"\s+", " ", value).strip()
    if not title or len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"Title must be 1-{MAX_TITLE_CHARS} characters.")
    if not TITLE_RE.match(title) or not slugify(title):
        raise ValueError("Title has unsupported characters (letters, numbers, spaces and - . , : & ' + / ( ) # ? ! are allowed).")
    return title


def clean_code(value, required=True):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError("Solution code is required (or switch on Manual entry).")
        return ""
    if not isinstance(value, str):
        raise ValueError("Solution code must be text.")
    code = value.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in code:
        raise ValueError("Code contains invalid characters.")
    if len(code) > MAX_CODE_CHARS:
        raise ValueError(f"Code is too long (max {MAX_CODE_CHARS:,} characters).")
    return code


def clean_difficulty(value):
    value = (value or "").strip().lower() if isinstance(value, str) or value is None else None
    if value not in DIFFICULTIES:
        raise ValueError("Difficulty must be easy, medium or hard.")
    return value


def clean_url(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > 300 or not URL_RE.match(value.strip()):
        raise ValueError("Problem URL must be a https://www.deep-ml.com/... link.")
    return value.strip()


def clean_note(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError("Invalid commit note.")
    note = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    if len(note) > MAX_NOTE_CHARS:
        raise ValueError(f"Commit note must be at most {MAX_NOTE_CHARS} characters.")
    return note


def clean_bool(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError("Options must be true or false.")


def parse_submission(data):
    manual = clean_bool(data.get("manual"), False)
    return {
        "title": clean_title(data.get("title")),
        "code": clean_code(data.get("code"), required=not manual),
        "difficulty": clean_difficulty(data.get("difficulty")),
        "url": clean_url(data.get("url")),
        "note": clean_note(data.get("note")),
        "solved": clean_bool(data.get("solved"), True),
        "manual": manual,
    }


RUN_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def parse_run_at(value):
    if not isinstance(value, str) or not RUN_AT_RE.fullmatch(value):
        raise ValueError("Pick a valid date and time.")
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("Pick a valid date and time.")
    now = datetime.now(timezone.utc)
    if dt < now + timedelta(seconds=30):
        raise ValueError("Pick a time at least a minute in the future.")
    if dt > now + timedelta(days=60):
        raise ValueError("You can schedule at most 60 days ahead.")
    return dt


# --------------------------------------------------------------------------
# File format + README index
# --------------------------------------------------------------------------
# solutions/<slug>.py starts with a small header:
#   """Title (difficulty)
#   https://www.deep-ml.com/...        (optional)
#   status: solved | unsolved
#   entry: manual                      (only for entries without code)
#   """
HEADER_RE = re.compile(r'\A"""(.*?)\n"""\n\n', re.S)
MANUAL_PLACEHOLDER = "# Manual entry: solution code has not been added yet."


def build_content(title, code, difficulty, url, solved, manual):
    head = title + (f" ({difficulty})" if difficulty else "")
    if url:
        head += "\n" + url
    head += f"\nstatus: {'solved' if solved else 'unsolved'}"
    if manual:
        head += "\nentry: manual"
    body = MANUAL_PLACEHOLDER if manual else code.rstrip()
    return f'"""{head}\n"""\n\n{body}\n'


def parse_content(text):
    m = HEADER_RE.match(text)
    if not m:
        return {"title": "", "code": text, "difficulty": "", "url": "", "solved": True, "manual": False}
    lines = m.group(1).split("\n")
    d = re.search(r"\((easy|medium|hard)\)$", lines[0])
    url, solved, manual = "", True, False
    for line in lines[1:]:
        line = line.strip()
        if line.startswith("https://"):
            url = line
        elif line.startswith("status:"):
            solved = line.split(":", 1)[1].strip() != "unsolved"
        elif line == "entry: manual":
            manual = True
    return {
        "title": re.sub(r"\s*\((easy|medium|hard)\)$", "", lines[0]),
        "code": "" if manual else text[m.end():],
        "difficulty": d.group(1) if d else "",
        "url": url,
        "solved": solved,
        "manual": manual,
    }


def problem_record(slug, title, difficulty, solved, manual, url):
    return {
        "slug": slug,
        "title": title,
        "difficulty": difficulty,
        "solved": solved,
        "manual": manual,
        "url": url,
        "file_url": f"https://github.com/{REPO}/blob/{BRANCH}/solutions/{slug}.py",
    }


def readme_entry(title, slug, difficulty, solved, manual, url):
    parts = [f"- [{title}](solutions/{slug}.py)"]
    if difficulty:
        parts.append(difficulty)
    parts.append("solved" if solved else "unsolved")
    if manual:
        parts.append("manual")
    if url:
        parts.append(f"[problem]({url})")
    return " · ".join(parts)


ENTRY_RE = re.compile(r"^- \[(.+?)\]\(solutions/([a-z0-9_]+)\.py\)(.*)$")


def parse_readme(text):
    items = []
    for line in text.split("\n"):
        m = ENTRY_RE.match(line)
        if not m:
            continue
        title, slug, rest = m.groups()
        item = problem_record(slug, title, "", True, False, "")
        for part in (p.strip() for p in rest.split(" · ")):
            if part in ("easy", "medium", "hard"):
                item["difficulty"] = part
            elif part == "unsolved":
                item["solved"] = False
            elif part == "manual":
                item["manual"] = True
            else:
                u = re.match(r"\[problem\]\((https://[^)\s]+)\)$", part)
                if u:
                    item["url"] = u.group(1)
        items.append(item)
    return items


def update_readme(title, slug, difficulty, url, solved, manual):
    entry = readme_entry(title, slug, difficulty, solved, manual, url)
    marker = f"](solutions/{slug}.py)"
    for attempt in range(2):
        text, sha = get_file("README.md")
        if text is None:
            text = "# Deep-ML Solutions\n\n## Index\n"
        lines = text.split("\n")
        idx = next((i for i, l in enumerate(lines) if l.startswith("- [") and marker in l), None)
        if idx is None:
            new = text.rstrip("\n") + "\n" + entry + "\n"
        elif lines[idx] == entry:
            return False
        else:
            lines[idx] = entry
            new = "\n".join(lines)
        try:
            put_file("README.md", new, f"Index: {title} ({date.today()})", sha)
            return True
        except GitHubError as exc:
            if exc.status in (409, 422) and attempt == 0:
                continue
            raise
    return False


def list_problems():
    slugs = list_solutions()
    on_disk = set(slugs)
    text, _ = get_file("README.md")
    out, seen = [], set()
    for item in reversed(parse_readme(text or "")):  # newest first
        if item["slug"] in on_disk and item["slug"] not in seen:
            out.append(item)
            seen.add(item["slug"])
    for slug in slugs:  # files that aren't in the README index yet
        if slug not in seen:
            out.append(problem_record(slug, slug.replace("_", " ").title(), "", True, False, ""))
    return out


# --------------------------------------------------------------------------
# Activity (commit history of solutions/, used for streaks + heatmap)
# --------------------------------------------------------------------------
ACTIVITY_DAYS = 365
ACTIVITY_MAX_PAGES = 5            # 5 x 100 = latest 500 solution commits
ACTIVITY_TTL = 60                 # seconds
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_activity_cache = {"at": 0.0, "data": None}


def _commit_page(since, page):
    try:
        r = SESSION.get(
            f"https://api.github.com/repos/{REPO}/commits",
            params={"sha": BRANCH, "path": "solutions", "since": since, "per_page": 100, "page": page},
            timeout=(5, 20),
        )
    except requests.RequestException as exc:
        log.warning("GitHub network error: %s", type(exc).__name__)
        raise GitHubError(0)
    if r.status_code == 409:  # empty repository
        return [], 0
    if not r.ok:
        log.warning("GitHub commits -> %s", r.status_code)
        raise GitHubError(r.status_code)
    items = r.json()
    dates = []
    for c in items:
        d = ((c.get("commit") or {}).get("author") or {}).get("date")
        if isinstance(d, str) and ISO_RE.fullmatch(d):
            dates.append(d)
    return dates, len(items)


def list_activity():
    now = time.time()
    with _lock:
        hit = _activity_cache["data"]
        if hit is not None and now - _activity_cache["at"] < ACTIVITY_TTL:
            return hit
    since = (datetime.now(timezone.utc) - timedelta(days=ACTIVITY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamps, truncated = [], False
    for page in range(1, ACTIVITY_MAX_PAGES + 1):
        dates, n = _commit_page(since, page)
        stamps.extend(dates)
        if n < 100:
            break
    else:
        truncated = True
    data = {"timestamps": stamps, "truncated": truncated}
    with _lock:
        _activity_cache.update(at=now, data=data)
    return data


def _bust_activity():
    with _lock:
        _activity_cache["data"] = None


# --------------------------------------------------------------------------
# Core sync logic
# --------------------------------------------------------------------------
def _retry_conflict(exc, attempt):
    return exc.status in (409, 422) and attempt == 0


def sync(title, code, difficulty, url, note, solved):
    slug = slugify(title)  # same title -> same file, so re-commits never create copies
    path = f"solutions/{slug}.py"
    commit_url, status = "", "unchanged"
    use_title, use_url, is_manual = title, url, False

    for attempt in range(2):
        old, sha = get_file(path)
        prev = parse_content(old) if old else None
        use_title = prev["title"] if prev and prev["title"] else title  # keep the original title
        use_url = url or (prev["url"] if prev else "")
        if code.strip():
            body, is_manual = code, False
        elif prev and not prev["manual"] and prev["code"].strip():
            body, is_manual = prev["code"], False  # manual update keeps the saved code
        else:
            body, is_manual = "", True
        content = build_content(use_title, body, difficulty, use_url, solved, is_manual)
        if old == content:
            status = "unchanged"
            break
        verb = "Update" if sha else ("Add" if is_manual else "Solve")
        message = f"{verb}: {use_title}" + (f" - {note}" if note else "")
        try:
            commit_url = put_file(path, content, message, sha)
            status = "updated" if sha else "created"
            break
        except GitHubError as exc:
            if _retry_conflict(exc, attempt):
                continue
            raise

    if status != "unchanged":
        _bust_activity()
    warnings = []
    try:
        update_readme(use_title, slug, difficulty, use_url, solved, is_manual)
    except GitHubError:
        warnings.append("Saved, but the README index could not be updated.")
    log.info("sync %s -> %s", path, status)
    return {
        "status": status,
        "path": path,
        "commit_url": commit_url,
        "warnings": warnings,
        "problem": problem_record(slug, use_title, difficulty, solved, is_manual, use_url),
    }


def set_status(slug, solved):
    path = f"solutions/{slug}.py"
    commit_url, status, rec = "", "unchanged", None
    for attempt in range(2):
        old, sha = get_file(path)
        if old is None:
            raise LookupError(slug)
        prev = parse_content(old)
        title = prev["title"] or slug.replace("_", " ").title()
        rec = problem_record(slug, title, prev["difficulty"], solved, prev["manual"], prev["url"])
        if prev["solved"] == solved and prev["title"]:
            break
        content = build_content(title, prev["code"], prev["difficulty"], prev["url"], solved, prev["manual"])
        try:
            commit_url = put_file(path, content, f"Mark {'solved' if solved else 'unsolved'}: {title}", sha)
            status = "updated"
            break
        except GitHubError as exc:
            if _retry_conflict(exc, attempt):
                continue
            raise
    if status == "updated":
        _bust_activity()
    warnings = []
    try:
        update_readme(rec["title"], slug, rec["difficulty"], rec["url"], solved, rec["manual"])
    except GitHubError:
        warnings.append("Saved, but the README index could not be updated.")
    log.info("status %s -> %s", path, "solved" if solved else "unsolved")
    return {"status": status, "path": path, "commit_url": commit_url, "warnings": warnings, "problem": rec}


# --------------------------------------------------------------------------
# Scheduled commits. The queue lives on a NON-default branch (QUEUE_BRANCH), so
# queueing never appears on your contribution graph. Publishing later makes the
# real commit on the default branch, dated at publish time (nothing is backdated).
# One file per item: queue/<YYYYMMDDTHHMMSSZ>_<id>_<slug>.json
# --------------------------------------------------------------------------
QUEUE_DIR = "queue"
MAX_PENDING = 30
MAX_PER_RUN = 5
QUEUE_NAME_RE = re.compile(r"(\d{8}T\d{6}Z)_([0-9a-f]{8})_([a-z0-9_]{1,100})\.json")
ID_RE = re.compile(r"[0-9a-f]{8}")
_run_lock = threading.Lock()


class QueueFull(Exception):
    pass


class Busy(Exception):
    pass


def _compact_to_iso(c):
    return f"{c[0:4]}-{c[4:6]}-{c[6:8]}T{c[9:11]}:{c[11:13]}:{c[13:15]}Z"


def ensure_queue_branch():
    r = _git("GET", f"ref/heads/{QUEUE_BRANCH}")
    if r.ok:
        return
    if r.status_code != 404:
        raise GitHubError(r.status_code)
    base = _git("GET", f"ref/heads/{BRANCH}")
    if not base.ok:
        raise GitHubError(base.status_code)
    c = _git("POST", "refs", json={"ref": f"refs/heads/{QUEUE_BRANCH}", "sha": base.json()["object"]["sha"]})
    if not c.ok and c.status_code != 422:  # 422 = created by a parallel request
        raise GitHubError(c.status_code)


def list_queue():
    r = _gh("GET", QUEUE_DIR, params={"ref": QUEUE_BRANCH})
    if r.status_code == 404:
        return []
    if not r.ok:
        raise GitHubError(r.status_code)
    out = []
    for it in r.json():
        m = QUEUE_NAME_RE.fullmatch(it.get("name", ""))
        if m and it.get("type") == "file":
            out.append({
                "id": m.group(2),
                "run_at": _compact_to_iso(m.group(1)),
                "slug": m.group(3),
                "title": m.group(3).replace("_", " ").title(),
                "name": it["name"],
                "sha": it["sha"],
            })
    return sorted(out, key=lambda e: (e["run_at"], e["id"]))


def _public(entry):
    return {k: entry[k] for k in ("id", "run_at", "slug", "title")}


def enqueue(item, run_at):
    ensure_queue_branch()
    if len(list_queue()) >= MAX_PENDING:
        raise QueueFull()
    qid = secrets.token_hex(4)
    stamp = run_at.strftime("%Y%m%dT%H%M%SZ")
    slug = slugify(item["title"])
    name = f"{stamp}_{qid}_{slug}.json"
    put_file(f"{QUEUE_DIR}/{name}", json.dumps(item), f"Schedule: {item['title']}", branch=QUEUE_BRANCH)
    return {"id": qid, "run_at": _compact_to_iso(stamp), "slug": slug, "title": slug.replace("_", " ").title()}


def _drop(path, sha, message):
    try:
        delete_file(path, sha, message, QUEUE_BRANCH)
    except GitHubError:
        log.warning("could not remove queue item %s", path)  # next run is idempotent (unchanged)


def publish_entry(entry):
    path = f"{QUEUE_DIR}/{entry['name']}"
    text, sha = get_file(path, QUEUE_BRANCH)
    if text is None:
        raise LookupError(entry["id"])
    try:
        item = parse_submission(json.loads(text))   # re-validated: never trust stored data
    except (ValueError, TypeError, AttributeError):
        _drop(path, sha, "Discard invalid scheduled item")
        raise ValueError("invalid scheduled item")
    result = sync(item["title"], item["code"], item["difficulty"], item["url"], item["note"], item["solved"])
    _drop(path, sha, f"Published: {item['title']}")
    return result


def run_due(only_id=None):
    if not _run_lock.acquire(timeout=25):
        raise Busy()
    try:
        pending = list_queue()
        if only_id:
            due = [e for e in pending if e["id"] == only_id]
            if not due:
                raise LookupError(only_id)
            return {"result": publish_entry(due[0])}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        due = [e for e in pending if e["run_at"] <= now]
        batch = due[:MAX_PER_RUN]
        published, failed = [], []
        for e in batch:
            try:
                res = publish_entry(e)
                published.append({"title": res["problem"]["title"], "path": res["path"], "status": res["status"]})
            except LookupError:
                continue
            except ValueError:
                failed.append({"id": e["id"], "title": e["title"], "error": "Invalid item was discarded."})
            except GitHubError as exc:
                failed.append({"id": e["id"], "title": e["title"], "error": gh_message(exc.status)})
                if exc.status in (401, 403):
                    break  # token problem: don't hammer GitHub
        log.info("run-due: %d published, %d failed, %d still due", len(published), len(failed), len(due) - len(batch))
        return {"published": published, "failed": failed, "remaining": len(due) - len(batch)}
    finally:
        _run_lock.release()


# --------------------------------------------------------------------------
# Security: HTTPS, headers, auth, throttling
# --------------------------------------------------------------------------
_lock = threading.Lock()
_fails = defaultdict(deque)
_hits = defaultdict(deque)
FAIL_LIMIT, FAIL_WINDOW = 5, 15 * 60       # 5 bad keys / 15 min / IP -> locked
REQ_LIMIT, REQ_WINDOW = 60, 60            # 60 requests / min / IP


def _prune(store, ip, window, now):
    dq = store[ip]
    while dq and now - dq[0] > window:
        dq.popleft()
    if not dq:
        store.pop(ip, None)
        return store[ip]
    return dq


def err(message, status=400, **extra):
    return jsonify(ok=False, error=message, **extra), status


def protected(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        # Browsers send Origin on cross-site requests; refuse anything not same-origin.
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return err("Cross-origin requests are not allowed.", 403)

        ip = request.remote_addr or "unknown"
        now = time.time()
        with _lock:
            fails = _prune(_fails, ip, FAIL_WINDOW, now)
            if len(fails) >= FAIL_LIMIT:
                wait = int(FAIL_WINDOW - (now - fails[0])) + 1
                return err(f"Too many failed attempts. Try again in {-(-wait // 60)} min.", 429, retry_after=wait)
            hits = _prune(_hits, ip, REQ_WINDOW, now)
            if len(hits) >= REQ_LIMIT:
                return err("Too many requests. Slow down for a minute.", 429, retry_after=REQ_WINDOW)
            hits.append(now)

        supplied = request.headers.get("X-API-Key", "")
        digest = hashlib.sha256(supplied.encode()).digest()
        if not hmac.compare_digest(digest, APP_KEY_HASH):  # constant-time
            with _lock:
                _fails[ip].append(now)
            log.warning("auth failure from %s", ip)
            return err("Invalid app key.", 401)
        return view(*args, **kwargs)

    return wrapper


@app.before_request
def _before():
    g.nonce = secrets.token_urlsafe(16)
    if request.path != "/healthz" and not request.is_secure and not _env("ALLOW_HTTP"):
        return redirect(request.url.replace("http://", "https://", 1), code=301)


@app.after_request
def _headers(resp):
    n = getattr(g, "nonce", "")
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        f"script-src 'nonce-{n}'; style-src 'nonce-{n}'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'; upgrade-insecure-requests"
    )
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(413)
def _too_big(_):
    return err("Request is too large.", 413)


@app.errorhandler(404)
def _not_found(_):
    return err("Not found.", 404)


@app.errorhandler(405)
def _bad_method(_):
    return err("Method not allowed.", 405)


@app.errorhandler(Exception)
def _boom(exc):
    log.exception("unhandled error: %s", type(exc).__name__)
    return err("Unexpected server error.", 500)


GH_MESSAGES = {
    0: "Couldn't reach GitHub. Try again in a moment.",
    401: "GitHub rejected the token (expired or revoked). Generate a new one.",
    403: "GitHub denied access or hit a rate limit. Check the token's Contents permission.",
    404: "Repo or branch not found, or the token can't access it.",
    409: "The file changed on GitHub while saving. Try again.",
    422: "GitHub couldn't process the commit. Try again.",
}


def gh_message(status):
    return GH_MESSAGES.get(status, "GitHub returned an error.")


def github_failure(exc):
    return err(gh_message(exc.status), 502)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/")
def home():
    return render_template_string(
        PAGE,
        nonce=g.nonce,
        year=date.today().year,
        github_url=GITHUB_URL,
        repo_url=REPO_URL,
        linkedin_url=LINKEDIN_URL,
        credly_url=CREDLY_URL,
        max_code=MAX_CODE_CHARS,
    )


@app.post("/submit")
@protected
def submit():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return err("Send a JSON body with Content-Type: application/json.", 400)
    try:
        item = parse_submission(data)
    except ValueError as exc:
        return err(str(exc), 400)
    try:
        result = sync(item["title"], item["code"], item["difficulty"], item["url"], item["note"], item["solved"])
    except GitHubError as exc:
        return github_failure(exc)
    return jsonify(ok=True, **result)


@app.post("/api/status")
@protected
def status_route():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return err("Send a JSON body with Content-Type: application/json.", 400)
    slug, solved = data.get("slug"), data.get("solved")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        return err("Invalid problem name.", 400)
    if not isinstance(solved, bool):
        return err("'solved' must be true or false.", 400)
    try:
        result = set_status(slug, solved)
    except LookupError:
        return err("No saved solution for that problem yet.", 404)
    except GitHubError as exc:
        return github_failure(exc)
    return jsonify(ok=True, **result)


@app.get("/api/problems")
@protected
def problems():
    try:
        return jsonify(ok=True, problems=list_problems())
    except GitHubError as exc:
        return github_failure(exc)


@app.post("/api/schedule")
@protected
def schedule_create():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return err("Send a JSON body with Content-Type: application/json.", 400)
    try:
        item = parse_submission(data)
        run_at = parse_run_at(data.get("run_at"))
    except ValueError as exc:
        return err(str(exc), 400)
    try:
        entry = enqueue(item, run_at)
    except QueueFull:
        return err(f"You already have {MAX_PENDING} scheduled commits. Publish or cancel some first.", 409)
    except GitHubError as exc:
        return github_failure(exc)
    log.info("scheduled %s for %s", entry["slug"], entry["run_at"])
    return jsonify(ok=True, item=entry)


@app.get("/api/schedule")
@protected
def schedule_list():
    try:
        return jsonify(ok=True, items=[_public(e) for e in list_queue()])
    except GitHubError as exc:
        return github_failure(exc)


@app.delete("/api/schedule/<sid>")
@protected
def schedule_cancel(sid):
    if not ID_RE.fullmatch(sid):
        return err("Invalid id.", 400)
    try:
        entry = next((e for e in list_queue() if e["id"] == sid), None)
        if not entry:
            return err("That scheduled commit no longer exists.", 404)
        delete_file(f"{QUEUE_DIR}/{entry['name']}", entry["sha"], f"Cancel: {entry['title']}", QUEUE_BRANCH)
    except GitHubError as exc:
        return github_failure(exc)
    return jsonify(ok=True)


@app.post("/api/schedule/<sid>/publish")
@protected
def schedule_publish(sid):
    if not ID_RE.fullmatch(sid):
        return err("Invalid id.", 400)
    try:
        return jsonify(ok=True, **run_due(only_id=sid)["result"])
    except LookupError:
        return err("That scheduled commit no longer exists.", 404)
    except ValueError:
        return err("That scheduled item was invalid and has been discarded.", 400)
    except Busy:
        return err("Another publish is running. Try again in a few seconds.", 409)
    except GitHubError as exc:
        return github_failure(exc)


@app.post("/api/run-due")
@protected
def run_due_route():
    try:
        return jsonify(ok=True, **run_due())
    except Busy:
        return err("Another publish is running.", 409)
    except GitHubError as exc:
        return github_failure(exc)


@app.get("/api/activity")
@protected
def activity():
    try:
        return jsonify(ok=True, **list_activity())
    except GitHubError as exc:
        return github_failure(exc)


@app.get("/api/solution")
@protected
def solution():
    slug = request.args.get("slug", "")
    if not SLUG_RE.match(slug):
        return err("Invalid problem name.", 400)
    try:
        text, _ = get_file(f"solutions/{slug}.py")
    except GitHubError as exc:
        return github_failure(exc)
    if text is None:
        return err("No saved solution for that problem yet.", 404)
    return jsonify(ok=True, **parse_content(text))


@app.get("/healthz")
def health():
    return "ok"


# Google Search Console "HTML file" verification. Put the file Google gives you
# (e.g. google1a2b3c4d5e6f7a8b.html) in the verification/ folder next to this file.
# Only filenames of that exact shape are served, nothing else can be read from disk.
VERIFY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verification")
GOOGLE_FILE_RE = re.compile(r"google[a-f0-9]{10,40}\.html")


@app.get("/<name>")
def google_verification(name):
    if not GOOGLE_FILE_RE.fullmatch(name):
        abort(404)
    return send_from_directory(VERIFY_DIR, name, mimetype="text/html")


# --------------------------------------------------------------------------
# UI (GitHub "Primer"-style, dark/light follows the system theme)
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<meta name="robots" content="noindex, nofollow">
<title>Deep-ML Sync</title>
<link rel="icon" href="data:,">
<style nonce="{{ nonce }}">
:root{--bg:#0d1117;--subtle:#161b22;--inset:#010409;--border:#30363d;--fg:#e6edf3;--muted:#8b949e;
--accent:#4493f8;--green:#238636;--green-h:#2ea043;--red:#f85149;--amber:#d29922;--blue:#1f6feb;
--btn:#21262d;--btn-h:#30363d;--shadow:0 8px 24px rgba(1,4,9,.6);
--h0:#151b23;--h1:#0e4429;--h2:#006d32;--h3:#26a641;--h4:#39d353}
@media (prefers-color-scheme:light){:root{--bg:#ffffff;--subtle:#f6f8fa;--inset:#f6f8fa;--border:#d0d7de;
--fg:#1f2328;--muted:#59636e;--accent:#0969da;--green:#1f883d;--green-h:#1a7f37;--red:#d1242f;--amber:#9a6700;
--blue:#0969da;--btn:#f6f8fa;--btn-h:#eaeef2;--shadow:0 8px 24px rgba(140,149,159,.35);
--h0:#eff2f5;--h1:#aceebb;--h2:#4ac26b;--h3:#2da44e;--h4:#116329}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0}
body{background:var(--bg);color:var(--fg);min-height:100vh;display:flex;flex-direction:column;
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.topbar{background:var(--subtle);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;align-items:center;gap:12px}
.logo{display:flex;border-radius:50%}
.logo:focus-visible,.crumb:focus-visible{outline:2px solid var(--blue);outline-offset:3px}
.logo svg{width:32px;height:32px;fill:var(--fg)}
.logo:hover svg{fill:var(--accent)}
.crumb{font-size:16px;color:var(--muted)}.crumb b{color:var(--fg);font-weight:600}
.crumb:hover{text-decoration:none;color:var(--accent)}.crumb:hover b{color:var(--accent)}
main{flex:1;width:100%;max-width:800px;margin:0 auto;padding:32px 16px}
h1{font-size:24px;font-weight:600;margin:0 0 4px;padding-bottom:8px}
.lead{color:var(--muted);margin:0 0 24px}
.box{border:1px solid var(--border);border-radius:6px;background:var(--bg)}
.box + .box{margin-top:24px}
.box-header{background:var(--subtle);border-bottom:1px solid var(--border);padding:12px 16px;font-weight:600;
display:flex;align-items:center;gap:8px;border-radius:6px 6px 0 0;flex-wrap:wrap}
.box-header svg{width:16px;height:16px;fill:var(--muted)}
.box-header .grow{flex:1}
.box-body{padding:16px}
.box-footer{border-top:1px solid var(--border);background:var(--subtle);padding:12px 16px;display:flex;
gap:8px;justify-content:flex-end;flex-wrap:wrap;border-radius:0 0 6px 6px}
.field{margin-bottom:16px}.field:last-child{margin-bottom:0}
.row{display:grid;grid-template-columns:180px 1fr;gap:16px}
@media (max-width:600px){.row{grid-template-columns:1fr}}
label{display:block;font-weight:600;margin-bottom:6px}
.opt{font-weight:400;color:var(--muted)}
.help{color:var(--muted);font-size:12px;margin:6px 0 0}
input,select,textarea{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);
border-radius:6px;padding:5px 12px;font:inherit;line-height:20px;min-height:32px}
input::placeholder,textarea::placeholder{color:var(--muted)}
input:focus,select:focus,textarea:focus{outline:2px solid var(--blue);outline-offset:-1px;border-color:var(--blue)}
.editor{border:1px solid var(--border);border-radius:6px;overflow:hidden}
.editor:focus-within{outline:2px solid var(--blue);outline-offset:-1px;border-color:var(--blue)}
.editor-head{background:var(--subtle);border-bottom:1px solid var(--border);padding:8px 12px;display:flex;
justify-content:space-between;color:var(--muted);font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.editor textarea{border:0;border-radius:0;outline:0!important;height:320px;resize:vertical;background:var(--inset);
font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:12px;tab-size:4;white-space:pre}
.btn{appearance:none;border:1px solid var(--border);background:var(--btn);color:var(--fg);border-radius:6px;
padding:5px 16px;font:inherit;font-weight:500;line-height:20px;cursor:pointer;display:inline-flex;align-items:center;gap:8px}
.btn:hover{background:var(--btn-h)}
.btn.sm{padding:2px 10px;font-size:12px;font-weight:500}
.btn.primary{background:var(--green);border-color:rgba(240,246,252,.1);color:#fff}
.btn.primary:hover{background:var(--green-h)}
.btn:disabled{opacity:.6;cursor:not-allowed}
.btn:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.spin{width:14px;height:14px;border:2px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;
animation:r .7s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}
.flash{display:flex;gap:8px;align-items:flex-start;border:1px solid var(--blue);background:rgba(31,111,235,.12);
border-radius:6px;padding:10px 12px;margin-top:8px;font-size:13px}
.flash svg{width:16px;height:16px;flex:none;margin-top:2px;fill:none;stroke:var(--accent);stroke-width:1.6}
code{font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--subtle);border:1px solid var(--border);
border-radius:4px;padding:1px 5px}
/* toggle switch */
.toggles{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media (max-width:600px){.toggles{grid-template-columns:1fr}}
.toggle{display:flex;justify-content:space-between;align-items:center;gap:12px;border:1px solid var(--border);
border-radius:6px;padding:10px 12px;background:var(--subtle)}
.toggle b{display:block}.toggle .d{color:var(--muted);font-size:12px}
.switch{position:relative;display:inline-block;width:40px;height:24px;flex:none;margin:0}
.switch input{position:absolute;inset:0;width:100%;height:100%;opacity:0;margin:0;cursor:pointer;z-index:1;min-height:0}
.switch .track{position:absolute;inset:0;border-radius:12px;background:var(--btn-h);border:1px solid var(--border);transition:.15s}
.switch .track::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;
background:var(--muted);transition:.15s}
.switch input:checked + .track{background:var(--green);border-color:var(--green)}
.switch input:checked + .track::after{left:19px;background:#fff}
.switch input:focus-visible + .track{outline:2px solid var(--blue);outline-offset:2px}
.switch input:disabled{cursor:wait}.switch input:disabled + .track{opacity:.5}
/* problems list */
.count{background:var(--btn-h);border-radius:2em;padding:0 8px;font-size:12px;line-height:18px;font-weight:500}
.seg{display:inline-flex;border:1px solid var(--border);border-radius:6px;overflow:hidden}
.seg button{background:var(--btn);border:0;border-right:1px solid var(--border);padding:2px 12px;font:inherit;
font-size:12px;font-weight:500;color:var(--fg);cursor:pointer}
.seg button:last-child{border-right:0}
.seg button[aria-pressed="true"]{background:var(--blue);color:#fff}
.plist{list-style:none;margin:0;padding:0}
.prow{display:flex;align-items:center;gap:12px;padding:10px 16px;border-top:1px solid var(--border)}
.prow:first-child{border-top:0}
.dot{flex:none;display:flex}.dot svg{width:16px;height:16px}
.dot.solved{color:var(--green-h)}.dot.unsolved{color:var(--muted)}
.pmain{flex:1;min-width:0}
.ptitle{background:none;border:0;color:var(--fg);font:inherit;font-weight:600;padding:0;cursor:pointer;text-align:left;word-break:break-word}
.ptitle:hover{color:var(--accent);text-decoration:underline}
.pmeta{display:flex;gap:6px;margin-top:2px;flex-wrap:wrap}
.chip{border:1px solid var(--border);border-radius:2em;padding:0 8px;font-size:12px;line-height:18px;color:var(--muted)}
.chip.easy{color:var(--green-h);border-color:var(--green-h)}
.chip.medium{color:var(--amber);border-color:var(--amber)}
.chip.hard{color:var(--red);border-color:var(--red)}
.pgh{color:var(--muted);display:flex}.pgh:hover{color:var(--accent)}.pgh svg{width:16px;height:16px}
.slabel{font-size:12px;color:var(--muted);width:56px;text-align:right}
.empty{padding:24px 16px;text-align:center;color:var(--muted)}
/* stats */
.muted-s{color:var(--muted);font-weight:400;font-size:12px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
@media (max-width:600px){.stats{grid-template-columns:repeat(2,1fr)}}
.stat{border:1px solid var(--border);border-radius:6px;padding:12px;background:var(--subtle)}
.stat .sv{font-size:24px;font-weight:600;line-height:1.25}
.stat .sl{color:var(--muted);font-size:12px}
.sub{font-size:14px;font-weight:600;margin:24px 0 10px}
.drow{display:grid;grid-template-columns:64px 1fr 48px;align-items:center;gap:12px;margin-bottom:8px;font-size:13px}
.drow .n{text-align:right;color:var(--muted)}
.bar{height:8px;border-radius:4px;background:var(--btn-h);overflow:hidden}
.bar i{display:block;height:100%;border-radius:4px}
.bar .easy{background:var(--green-h)}.bar .medium{background:var(--amber)}.bar .hard{background:var(--red)}.bar .other{background:var(--accent)}
.heat-wrap{overflow-x:auto;padding-bottom:4px}
.months,.heat{display:grid;grid-auto-flow:column;grid-auto-columns:10px;gap:3px;width:max-content}
.months{grid-template-rows:14px;margin-bottom:4px;font-size:11px;color:var(--muted)}
.months span{white-space:nowrap}
.heat{grid-template-rows:repeat(7,10px)}
.cell{display:block;width:10px;height:10px;border-radius:2px;background:var(--h0);outline:1px solid rgba(128,128,128,.14);outline-offset:-1px}
.l1{background:var(--h1)}.l2{background:var(--h2)}.l3{background:var(--h3)}.l4{background:var(--h4)}
.legend{display:flex;align-items:center;justify-content:flex-end;gap:4px;font-size:12px;color:var(--muted);margin-top:8px}
.legend .cell{display:inline-block}
#act-msg{color:var(--muted)}
/* scheduling */
.sched{margin-top:12px}
.presets{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.stitle{font-weight:600;word-break:break-word}
/* toasts */
#toasts{position:fixed;top:16px;right:16px;display:flex;flex-direction:column;gap:8px;z-index:50;width:min(380px,calc(100vw - 32px))}
.toast{display:flex;gap:10px;align-items:flex-start;background:var(--subtle);border:1px solid var(--border);
border-left-width:4px;border-radius:6px;padding:12px;box-shadow:var(--shadow);animation:in .2s ease-out}
.toast.out{opacity:0;transform:translateX(12px);transition:.2s}
@keyframes in{from{opacity:0;transform:translateX(16px)}to{opacity:1;transform:none}}
.toast.success{border-left-color:var(--green-h)}.toast.error{border-left-color:var(--red)}
.toast.info{border-left-color:var(--accent)}.toast.warning{border-left-color:var(--amber)}
.t-icon{flex:none;margin-top:1px}.t-icon svg{width:18px;height:18px;display:block}
.success .t-icon{color:var(--green-h)}.error .t-icon{color:var(--red)}.info .t-icon{color:var(--accent)}.warning .t-icon{color:var(--amber)}
.t-body{flex:1;min-width:0}.t-body strong{display:block}.t-body p{margin:2px 0 0;color:var(--muted);word-wrap:anywhere}
.t-body a{display:inline-block;margin-top:4px;font-size:13px}
.t-close{background:none;border:0;color:var(--muted);font-size:18px;line-height:1;cursor:pointer;padding:0 2px}
.t-close:hover{color:var(--fg)}
/* footer */
footer{border-top:1px solid var(--border);background:var(--subtle);margin-top:32px}
.f-inner{max-width:800px;margin:0 auto;padding:32px 16px 16px;display:grid;grid-template-columns:1.4fr 1fr;gap:24px}
@media (max-width:600px){.f-inner{grid-template-columns:1fr}}
.f-brand{display:flex;align-items:center;gap:8px;font-weight:600;margin-bottom:8px}
.f-brand svg{width:24px;height:24px;fill:var(--fg)}
footer p{margin:0 0 8px;color:var(--muted);font-size:12px}
footer h2{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 10px}
.social{display:flex;flex-direction:column;gap:8px}
.social a{display:inline-flex;align-items:center;gap:10px;color:var(--fg)}
.social svg{width:18px;height:18px;fill:currentColor}
.social a:hover{color:var(--accent);text-decoration:none}
.f-bottom{max-width:800px;margin:0 auto;padding:12px 16px 24px;color:var(--muted);font-size:12px;border-top:1px solid var(--border)}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.badge{border:1px solid var(--border);border-radius:2em;padding:0 8px;font-size:12px;color:var(--muted);line-height:18px}
</style>
</head>
<body>
<header class="topbar">
  <a class="logo" href="{{ github_url }}" target="_blank" rel="noopener noreferrer" aria-label="GitHub profile (home)" title="GitHub profile">
    <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>
  </a>
  <a class="crumb" href="{{ repo_url }}" target="_blank" rel="noopener noreferrer" title="Open the solutions repository">Deep-ML <span>/</span> <b>Sync</b></a>
</header>

<main>
  <h1>Commit a solution</h1>
  <p class="lead">Push your Deep-ML solution to GitHub. Committing the same problem again updates its existing file instead of creating a copy.</p>

  <form id="f" class="box" autocomplete="off" novalidate>
    <div class="box-header">
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M11.93 8.5a4.002 4.002 0 0 1-7.86 0H.75a.75.75 0 0 1 0-1.5h3.32a4.002 4.002 0 0 1 7.86 0h3.32a.75.75 0 0 1 0 1.5Zm-1.43-.75a2.5 2.5 0 1 0-5 0 2.5 2.5 0 0 0 5 0Z"/></svg>
      New commit
    </div>
    <div class="box-body">
      <div class="field">
        <label for="key">App key</label>
        <input id="key" type="password" autocomplete="current-password" spellcheck="false" required>
        <p class="help">Sent over HTTPS in a request header. Never stored in the browser.</p>
      </div>

      <div class="field">
        <label for="title">Problem title</label>
        <input id="title" list="problems" placeholder="e.g. Single Neuron" maxlength="100" required>
        <datalist id="problems"></datalist>
        <div id="hint" class="flash" hidden>
          <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.5"/><path d="M8 7.2v4M8 4.8v.01" stroke-linecap="round"/></svg>
          <span id="hint-text"></span>
        </div>
      </div>

      <div class="row field">
        <div>
          <label for="difficulty">Difficulty</label>
          <select id="difficulty"><option value="easy">Easy</option><option value="medium">Medium</option><option value="hard">Hard</option></select>
        </div>
        <div>
          <label for="url">Problem URL <span class="opt">(optional)</span></label>
          <input id="url" type="url" placeholder="https://www.deep-ml.com/problems/..." maxlength="300">
        </div>
      </div>

      <div class="toggles field">
        <div class="toggle">
          <div><b>Solved</b><span class="d">Turn off if you haven't solved it yet.</span></div>
          <label class="switch"><input id="solved" type="checkbox" role="switch" checked aria-label="Solved"><span class="track"></span></label>
        </div>
        <div class="toggle">
          <div><b>Manual entry</b><span class="d">Log the problem without code.</span></div>
          <label class="switch"><input id="manual" type="checkbox" role="switch" aria-label="Manual entry"><span class="track"></span></label>
        </div>
      </div>

      <div class="field">
        <div class="toggle">
          <div><b>Schedule for later</b><span class="d">Save it now and publish to GitHub at a time you choose, e.g. the weekend.</span></div>
          <label class="switch"><input id="sched" type="checkbox" role="switch" aria-label="Schedule for later"><span class="track"></span></label>
        </div>
        <div class="sched" id="sched-panel" hidden>
          <label for="when">Publish at <span class="opt">(your local time)</span></label>
          <input id="when" type="datetime-local">
          <div class="presets">
            <button type="button" class="btn sm" data-p="sat">Next Saturday 10:00</button>
            <button type="button" class="btn sm" data-p="sun">Next Sunday 10:00</button>
            <button type="button" class="btn sm" data-p="tom">Tomorrow 09:00</button>
          </div>
          <p class="help" id="when-help"></p>
        </div>
      </div>

      <div class="field">
        <label for="code">Solution <span id="code-opt" class="opt" hidden>(optional for manual entries)</span></label>
        <div class="editor">
          <div class="editor-head"><span>solution.py</span><span id="count">0 / {{ max_code }}</span></div>
          <textarea id="code" spellcheck="false" placeholder="Paste your solution here"></textarea>
        </div>
      </div>

      <div class="field">
        <label for="note">Commit message <span class="opt">(optional)</span></label>
        <input id="note" placeholder="e.g. Vectorised with numpy" maxlength="100">
      </div>
    </div>
    <div class="box-footer">
      <button type="button" id="load" class="btn" hidden>Load current code</button>
      <button type="submit" id="go" class="btn primary">Commit changes</button>
    </div>
  </form>

  <section class="box" aria-labelledby="sch-h">
    <div class="box-header">
      <span id="sch-h">Scheduled</span> <span class="count" id="scount">0</span><span class="grow"></span>
      <span class="muted-s">Published automatically by GitHub Actions</span>
    </div>
    <ul class="plist" id="slist"></ul>
    <div class="empty" id="sempty">Enter your app key above to see scheduled commits.</div>
  </section>

  <section class="box" aria-labelledby="stats-h">
    <div class="box-header">
      <span id="stats-h">Stats</span><span class="grow"></span>
      <span class="muted-s">From your saved problems and commit history</span>
    </div>
    <div class="box-body">
      <div class="empty" id="stats-empty">Enter your app key above to see your stats.</div>
      <div id="stats-content" hidden>
        <div class="stats" id="stat-cards"></div>
        <h3 class="sub">By difficulty</h3>
        <div id="diff-bars"></div>
        <h3 class="sub" id="act-title">Activity</h3>
        <p id="act-msg" hidden></p>
        <div class="heat-wrap" id="heat-wrap" hidden>
          <div class="months" id="months"></div>
          <div class="heat" id="heat" role="img" aria-label="Solution commits over the last year"></div>
        </div>
        <div class="legend" id="legend" hidden>Less <i class="cell l0"></i><i class="cell l1"></i><i class="cell l2"></i><i class="cell l3"></i><i class="cell l4"></i> More</div>
      </div>
    </div>
  </section>

  <section class="box" aria-labelledby="ptitle-h">
    <div class="box-header">
      <span id="ptitle-h">Your problems</span> <span class="count" id="pcount">0</span>
      <span class="grow"></span>
      <div class="seg" id="seg" role="group" aria-label="Filter problems">
        <button type="button" data-f="all" aria-pressed="true">All</button>
        <button type="button" data-f="solved" aria-pressed="false">Solved</button>
        <button type="button" data-f="unsolved" aria-pressed="false">Unsolved</button>
      </div>
      <button type="button" id="refresh" class="btn sm">Refresh</button>
    </div>
    <ul class="plist" id="plist"></ul>
    <div class="empty" id="empty">Enter your app key above to load your problems.</div>
  </section>
</main>

<footer>
  <div class="f-inner">
    <div>
      <div class="f-brand">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>
        Deep-ML Sync
      </div>
      <p>Commit your Deep-ML solutions to GitHub so your practice shows up on your contribution graph. Each problem keeps a single file, and every re-commit is a new version in its history.</p>
      <div class="badges">
        <span class="badge">HTTPS only</span><span class="badge">No cookies</span>
        <span class="badge">Code is never stored on this server</span><span class="badge">Rate limited</span>
      </div>
    </div>
    <div>
      <h2>Built by Karan Jajoria</h2>
      <div class="social">
        <a href="{{ github_url }}" target="_blank" rel="noopener noreferrer">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>
          GitHub
        </a>
        <a href="{{ linkedin_url }}" target="_blank" rel="noopener noreferrer">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg>
          LinkedIn
        </a>
        <a href="{{ credly_url }}" target="_blank" rel="noopener noreferrer">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path fill-rule="evenodd" d="M12 1.5l9 5.2v10.6l-9 5.2-9-5.2V6.7l9-5.2zm0 4.3a6.2 6.2 0 100 12.4 6.2 6.2 0 000-12.4zm0 2.4a3.8 3.8 0 110 7.6 3.8 3.8 0 010-7.6z"/></svg>
          Credly
        </a>
      </div>
    </div>
  </div>
  <div class="f-bottom">&copy; {{ year }} Karan Jajoria. Independent project, not affiliated with Deep-ML or GitHub.</div>
</footer>

<div id="toasts" aria-live="polite"></div>

<script nonce="{{ nonce }}">
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const MAX_CODE = {{ max_code }};
  let problems = [], known = new Set(), lastKey = "", busy = false, unlocked = false, filter = "all", hintTimer,
      activity = null, activityMsg = "", activityTruncated = false,
      scheduled = null, schedMsg = "";

  const svg = (inner) => '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + inner + "</svg>";
  const ICONS = {
    success: svg('<circle cx="8" cy="8" r="6.5"/><path d="M5.2 8.2l2 2 3.6-4"/>'),
    error: svg('<circle cx="8" cy="8" r="6.5"/><path d="M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8"/>'),
    info: svg('<circle cx="8" cy="8" r="6.5"/><path d="M8 7.2v4M8 4.8v.01"/>'),
    warning: svg('<path d="M8 2L14.5 13.5H1.5Z"/><path d="M8 6.5v3.2M8 11.6v.01"/>')
  };
  const DOT_SOLVED = '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><circle cx="8" cy="8" r="7"/><path d="M4.8 8.2l2.2 2.2 4.2-4.4" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const DOT_OPEN = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="8" cy="8" r="6.5"/></svg>';
  const EXT = svg('<path d="M9 2h5v5M14 2L7.5 8.5M12 9.5V13a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h3.5"/>');

  function toast(kind, title, message, link) {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    const ic = document.createElement("span"); ic.className = "t-icon"; ic.innerHTML = ICONS[kind];
    const body = document.createElement("div"); body.className = "t-body";
    const b = document.createElement("strong"); b.textContent = title; body.appendChild(b);
    if (message) { const p = document.createElement("p"); p.textContent = message; body.appendChild(p); }
    if (link && link.startsWith("https://github.com/")) {
      const a = document.createElement("a");
      a.href = link; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = "View commit on GitHub";
      body.appendChild(a);
    }
    const x = document.createElement("button");
    x.type = "button"; x.className = "t-close"; x.setAttribute("aria-label", "Dismiss"); x.textContent = "\u00d7";
    const remove = () => { el.classList.add("out"); setTimeout(() => el.remove(), 200); };
    x.addEventListener("click", remove);
    el.append(ic, body, x);
    $("toasts").appendChild(el);
    setTimeout(remove, kind === "error" ? 9000 : 6000);
  }

  async function api(path, options) {
    options = options || {};
    const headers = { "X-API-Key": $("key").value };
    if (options.body) headers["Content-Type"] = "application/json";
    let res;
    try {
      res = await fetch(path, Object.assign({}, options, { headers: headers, cache: "no-store", credentials: "omit" }));
    } catch (e) { return { networkError: true }; }
    let data = {};
    try { data = await res.json(); } catch (e) {}
    return { res: res, data: data };
  }

  function failToast(r) {
    if (r.networkError) return toast("error", "Network error", "Couldn't reach the server. Check your connection and try again.");
    const s = r.res.status, m = r.data && r.data.error;
    if (s === 401) return toast("error", "Invalid app key", "The key doesn't match. Check it and try again.");
    if (s === 429) return toast("warning", "Slow down", m || "Too many attempts. Please wait.");
    if (s === 502) return toast("error", "GitHub problem", m || "GitHub rejected the request.");
    if (s === 404) return toast("warning", "Not found", m || "Nothing found.");
    toast("error", "Couldn't complete that", m || ("Unexpected error (" + s + ")."));
  }

  const slugify = (t) => t.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

  /* ---------- problems list ---------- */
  function makeSwitch(checked, label) {
    const l = document.createElement("label"); l.className = "switch";
    const i = document.createElement("input");
    i.type = "checkbox"; i.setAttribute("role", "switch"); i.checked = checked; i.setAttribute("aria-label", label);
    const t = document.createElement("span"); t.className = "track";
    l.append(i, t);
    return { el: l, input: i };
  }

  function row(p) {
    const li = document.createElement("li"); li.className = "prow";
    const dot = document.createElement("span");
    dot.className = "dot " + (p.solved ? "solved" : "unsolved");
    dot.innerHTML = p.solved ? DOT_SOLVED : DOT_OPEN;

    const main = document.createElement("div"); main.className = "pmain";
    const t = document.createElement("button");
    t.type = "button"; t.className = "ptitle"; t.textContent = p.title; t.title = "Load into the form";
    t.addEventListener("click", async () => {
      await loadInto(p.slug, p.title);
      $("f").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    const meta = document.createElement("div"); meta.className = "pmeta";
    if (p.difficulty) { const c = document.createElement("span"); c.className = "chip " + p.difficulty; c.textContent = p.difficulty; meta.appendChild(c); }
    if (p.manual) { const c = document.createElement("span"); c.className = "chip"; c.textContent = "manual"; meta.appendChild(c); }
    main.append(t, meta);

    li.append(dot, main);
    if (p.file_url && p.file_url.startsWith("https://github.com/")) {
      const a = document.createElement("a");
      a.className = "pgh"; a.href = p.file_url; a.target = "_blank"; a.rel = "noopener noreferrer";
      a.title = "Open on GitHub"; a.setAttribute("aria-label", "Open " + p.title + " on GitHub"); a.innerHTML = EXT;
      li.appendChild(a);
    }
    const label = document.createElement("span"); label.className = "slabel"; label.textContent = p.solved ? "Solved" : "Unsolved";
    const sw = makeSwitch(p.solved, "Solved: " + p.title);
    sw.input.addEventListener("change", async () => {
      const want = sw.input.checked;
      sw.input.disabled = true;
      const r = await api("/api/status", { method: "POST", body: JSON.stringify({ slug: p.slug, solved: want }) });
      sw.input.disabled = false;
      if (r.networkError || !r.res.ok) { sw.input.checked = !want; return failToast(r); }
      if (r.data.status === "updated") bumpActivity();
      upsert(r.data.problem);
      toast("success", want ? "Marked as solved" : "Marked as unsolved", p.title, r.data.commit_url);
      (r.data.warnings || []).forEach((w) => toast("warning", "Heads up", w));
    });
    li.append(label, sw.el);
    return li;
  }

  function renderList() {
    const list = $("plist"); list.replaceChildren();
    $("pcount").textContent = problems.length;
    document.querySelectorAll("#seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.f === filter)));
    const shown = problems.filter((p) => filter === "all" || (filter === "solved") === p.solved);
    shown.forEach((p) => list.appendChild(row(p)));
    $("empty").hidden = shown.length > 0;
    if (!shown.length) {
      $("empty").textContent = !unlocked ? "Enter your app key above to load your problems."
        : problems.length ? "No problems match this filter." : "No problems yet. Commit your first solution above.";
    }
  }

  function updateHint() {
    const slug = slugify($("title").value);
    const hit = slug && known.has(slug);
    $("hint").hidden = !hit; $("load").hidden = !hit;
    if (hit) $("hint-text").textContent = "A solution for this problem already exists. Committing will update solutions/" + slug + ".py; no copy will be created.";
  }

  function render() {
    known = new Set(problems.map((p) => p.slug));
    const dl = $("problems"); dl.replaceChildren();
    problems.forEach((p) => { const o = document.createElement("option"); o.value = p.title; dl.appendChild(o); });
    renderList(); updateHint(); renderStats(); renderScheduled();
  }

  function upsert(p) {
    const i = problems.findIndex((x) => x.slug === p.slug);
    if (i >= 0) problems[i] = p; else problems.unshift(p);
    render();
  }

  async function refreshList(announce) {
    const r = await api("/api/problems");
    if (r.networkError || !r.res.ok) {
      lastKey = ""; unlocked = false; problems = []; activity = null; scheduled = null; schedMsg = ""; render(); failToast(r); return false;
    }
    unlocked = true; problems = r.data.problems; render();
    if (announce) toast("info", "Unlocked", problems.length + " problem" + (problems.length === 1 ? "" : "s") + " found.");
    return true;
  }

  async function loadActivity() {
    const r = await api("/api/activity");
    if (r.networkError || !r.res.ok) {
      activity = null;
      activityMsg = r.networkError ? "Activity couldn't be loaded (network error)." : ((r.data && r.data.error) || "Activity couldn't be loaded.");
    } else {
      activity = r.data.timestamps || []; activityTruncated = !!r.data.truncated; activityMsg = "";
    }
    renderStats();
  }

  async function unlockAndLoad(announce) {
    const ok = await refreshList(announce);   // activity only loads after the key is accepted
    if (ok) await Promise.all([loadActivity(), loadScheduled()]);
    return ok;
  }

  /* ---------- stats ---------- */
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const dayKey = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function bumpActivity() {   // show a fresh commit immediately; GitHub's list can lag a few seconds
    if (Array.isArray(activity)) { activity.push(new Date().toISOString()); renderStats(); }
  }

  function streaks(counts) {
    const today = midnight(new Date());
    let longest = 0, run = 0;
    for (let i = 364; i >= 0; i--) {
      if (counts.get(dayKey(addDays(today, -i)))) { run++; if (run > longest) longest = run; } else run = 0;
    }
    let cur = 0, d = today;
    if (!counts.get(dayKey(d))) d = addDays(d, -1);   // today not done yet: streak still alive until midnight
    while (counts.get(dayKey(d))) { cur++; d = addDays(d, -1); }
    return { current: cur, longest: longest };
  }

  function drawHeat(counts) {
    const heat = $("heat"), months = $("months");
    heat.replaceChildren(); months.replaceChildren();
    const today = midnight(new Date());
    const start = addDays(today, -(52 * 7 + today.getDay()));   // a Sunday, 53 weekly columns like GitHub
    let lastMonth = -1;
    for (let d = new Date(start); d <= today; d = addDays(d, 1)) {
      if (d.getDay() === 0) {
        const label = el("span");
        if (d.getDate() <= 7 && d.getMonth() !== lastMonth) { label.textContent = MONTHS[d.getMonth()]; lastMonth = d.getMonth(); }
        months.appendChild(label);
      }
      const n = counts.get(dayKey(d)) || 0;
      const cell = el("i", "cell l" + (n === 0 ? 0 : n === 1 ? 1 : n === 2 ? 2 : n <= 4 ? 3 : 4));
      cell.title = plural(n, "commit") + " on " + d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
      heat.appendChild(cell);
    }
  }

  function renderStats() {
    $("stats-empty").hidden = unlocked;
    $("stats-content").hidden = !unlocked;
    if (!unlocked) return;

    const total = problems.length, solved = problems.filter((p) => p.solved).length;
    const manual = problems.filter((p) => p.manual).length;
    const counts = new Map();
    (activity || []).forEach((ts) => {
      const d = new Date(ts);
      if (!isNaN(d)) { const k = dayKey(d); counts.set(k, (counts.get(k) || 0) + 1); }
    });
    const st = streaks(counts);

    const cards = $("stat-cards"); cards.replaceChildren();
    const card = (v, l) => { const c = el("div", "stat"); c.append(el("div", "sv", v), el("div", "sl", l)); cards.appendChild(c); };
    card(solved + " / " + total, "Solved");
    card((total ? Math.round(solved * 100 / total) : 0) + "%", "Completion");
    card(String(total - solved), "Unsolved");
    card(String(manual), "Manual entries (no code)");
    card(activity ? plural(st.current, "day") : "\u2013", "Current streak");
    card(activity ? plural(st.longest, "day") : "\u2013", "Longest streak (last year)");

    const bars = $("diff-bars"); bars.replaceChildren();
    const addBar = (cls, label, list) => {
      const s = list.filter((p) => p.solved).length;
      const row = el("div", "drow"), track = el("div", "bar"), fill = el("i", cls);
      fill.style.width = (list.length ? s * 100 / list.length : 0) + "%";
      track.appendChild(fill);
      row.append(el("span", null, label), track, el("span", "n", s + "/" + list.length));
      bars.appendChild(row);
    };
    const levels = ["easy", "medium", "hard"];
    levels.forEach((lv) => addBar(lv, lv.charAt(0).toUpperCase() + lv.slice(1), problems.filter((p) => p.difficulty === lv)));
    const other = problems.filter((p) => levels.indexOf(p.difficulty) < 0);
    if (other.length) addBar("other", "Other", other);

    if (activity === null) {
      $("act-title").textContent = "Activity";
      $("heat-wrap").hidden = true; $("legend").hidden = true;
      $("act-msg").hidden = false; $("act-msg").textContent = activityMsg || "Loading activity\u2026";
      return;
    }
    $("act-msg").hidden = true; $("heat-wrap").hidden = false; $("legend").hidden = false;
    $("act-title").textContent = plural(activity.length, "commit") + " in the last year \u00b7 " +
      plural(counts.size, "active day") + (activityTruncated ? " (latest " + activity.length + " shown)" : "");
    drawHeat(counts);
  }

  /* ---------- scheduling ---------- */
  const CLOCK = svg('<circle cx="8" cy="8" r="6.5"/><path d="M8 4.5V8l2.5 1.5"/>');
  const fmtWhen = (iso) => new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  function rel(iso) {
    const ms = new Date(iso).getTime() - Date.now();
    if (ms <= 0) return "due now";
    const min = Math.round(ms / 60000);
    if (min < 60) return "in " + Math.max(min, 1) + " min";
    const h = Math.round(min / 60);
    return h < 48 ? "in " + h + " h" : "in " + Math.round(h / 24) + " days";
  }
  const toInput = (d) => dayKey(d) + "T" + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  function nextDow(dow, hour) {
    const n = new Date();
    const d = new Date(n.getFullYear(), n.getMonth(), n.getDate(), hour, 0);
    d.setDate(d.getDate() + ((dow - n.getDay() + 7) % 7));
    if (d <= n) d.setDate(d.getDate() + 7);
    return d;
  }
  function presetDate(p) {
    if (p === "sat") return nextDow(6, 10);
    if (p === "sun") return nextDow(0, 10);
    const n = new Date();
    return new Date(n.getFullYear(), n.getMonth(), n.getDate() + 1, 9, 0);
  }
  function goLabel() { return $("sched").checked ? "Schedule commit" : ($("manual").checked ? "Add manual entry" : "Commit changes"); }

  function updateWhen() {
    const v = $("when").value, t = v ? new Date(v) : null;
    $("when-help").textContent = (t && !isNaN(t))
      ? "Publishes around " + fmtWhen(t.toISOString()) + " (" + rel(t.toISOString()) + ", your local time). It's checked every 15 minutes, so it can land a little later."
      : "Pick a date and time, or use a shortcut.";
  }
  function applySched() {
    const on = $("sched").checked;
    $("sched-panel").hidden = !on;
    if (on) { $("when").min = toInput(new Date()); updateWhen(); }
    applyManual();
  }

  async function loadScheduled() {
    const r = await api("/api/schedule");
    if (r.networkError || !r.res.ok) {
      scheduled = null;
      schedMsg = r.networkError ? "Couldn't load scheduled commits (network error)." : ((r.data && r.data.error) || "Couldn't load scheduled commits.");
    } else { scheduled = r.data.items || []; schedMsg = ""; }
    renderScheduled();
  }

  function srow(s) {
    const li = el("li", "prow");
    const dot = el("span", "dot unsolved"); dot.innerHTML = CLOCK;
    const main = el("div", "pmain");
    const meta = el("div", "pmeta");
    meta.append(el("span", "chip", fmtWhen(s.run_at)), el("span", "chip", rel(s.run_at)));
    main.append(el("span", "stitle", s.title), meta);
    const pub = el("button", "btn sm", "Publish now"); pub.type = "button";
    const cancel = el("button", "btn sm", "Cancel"); cancel.type = "button";

    pub.addEventListener("click", async () => {
      pub.disabled = true; cancel.disabled = true;
      const r = await api("/api/schedule/" + s.id + "/publish", { method: "POST" });
      if (r.networkError || !r.res.ok) { pub.disabled = false; cancel.disabled = false; return failToast(r); }
      const d = r.data;
      scheduled = (scheduled || []).filter((x) => x.id !== s.id);
      if (d.status !== "unchanged") bumpActivity();
      if (d.problem) upsert(d.problem); else render();
      toast("success", "Published", d.path, d.commit_url);
      (d.warnings || []).forEach((w) => toast("warning", "Heads up", w));
    });
    cancel.addEventListener("click", async () => {
      if (!window.confirm("Cancel this scheduled commit?")) return;
      pub.disabled = true; cancel.disabled = true;
      const r = await api("/api/schedule/" + s.id, { method: "DELETE" });
      if (r.networkError || !r.res.ok) { pub.disabled = false; cancel.disabled = false; return failToast(r); }
      scheduled = (scheduled || []).filter((x) => x.id !== s.id);
      renderScheduled();
      toast("info", "Cancelled", s.title + " will not be published.");
    });
    li.append(dot, main, pub, cancel);
    return li;
  }

  function renderScheduled() {
    const list = $("slist"); list.replaceChildren();
    const items = scheduled || [];
    $("scount").textContent = items.length;
    items.forEach((s) => list.appendChild(srow(s)));
    $("sempty").hidden = items.length > 0;
    if (!items.length) {
      $("sempty").textContent = !unlocked ? "Enter your app key above to see scheduled commits."
        : scheduled === null ? (schedMsg || "Loading\u2026")
        : "Nothing scheduled. Turn on \u201cSchedule for later\u201d when committing.";
    }
  }

  async function submitScheduled(manual) {
    const v = $("when").value, t = v ? new Date(v) : null;
    if (!t || isNaN(t)) { toast("warning", "Pick a time", "Choose when this should be published."); return $("when").focus(); }
    if (t.getTime() < Date.now() + 60000) { toast("warning", "Pick a later time", "The time must be at least a minute from now."); return $("when").focus(); }
    setBusy(true, "Scheduling\u2026");
    try {
      const r = await api("/api/schedule", { method: "POST", body: JSON.stringify({
        title: $("title").value, code: $("code").value, difficulty: $("difficulty").value,
        url: $("url").value, note: $("note").value, solved: $("solved").checked, manual: manual,
        run_at: t.toISOString().replace(/\.\d{3}Z$/, "Z")
      })});
      if (r.networkError || !r.res.ok) return failToast(r);
      scheduled = (scheduled || []).concat([r.data.item]).sort((a, b) => a.run_at < b.run_at ? -1 : 1);
      renderScheduled();
      toast("success", "Scheduled", $("title").value.trim() + " will publish around " + fmtWhen(r.data.item.run_at) + ".");
      $("note").value = "";
    } finally { setBusy(false); }
  }

  /* ---------- form ---------- */
  function applyManual() {
    const m = $("manual").checked;
    $("code-opt").hidden = !m;
    $("code").placeholder = m ? "Optional: paste your solution, or leave empty to just log the problem" : "Paste your solution here";
    if (!busy) $("go").textContent = goLabel();
  }

  function setBusy(on, text) {
    busy = on;
    const go = $("go");
    go.disabled = on;
    go.replaceChildren();
    if (on) { const s = document.createElement("span"); s.className = "spin"; go.append(s, text || "Committing\u2026"); }
    else applyManual();
  }

  async function loadInto(slug, title) {
    const r = await api("/api/solution?slug=" + encodeURIComponent(slug));
    if (r.networkError || !r.res.ok) return failToast(r);
    const d = r.data;
    if (title) $("title").value = title;
    $("code").value = d.code || "";
    if (d.difficulty) $("difficulty").value = d.difficulty;
    $("url").value = d.url || "";
    $("solved").checked = d.solved;
    $("manual").checked = d.manual;
    $("code").dispatchEvent(new Event("input"));
    applyManual(); updateHint();
    toast("info", "Loaded", d.manual ? "Manual entry loaded. Add your code whenever you're ready." : "Current code is in the editor.");
  }

  $("key").addEventListener("change", () => {
    const k = $("key").value;
    if (!k) { lastKey = ""; unlocked = false; problems = []; activity = null; scheduled = null; render(); return; }
    if (k === lastKey) return;
    lastKey = k;
    unlockAndLoad(true);
  });
  $("title").addEventListener("input", () => { clearTimeout(hintTimer); hintTimer = setTimeout(updateHint, 150); });
  $("code").addEventListener("input", () => { $("count").textContent = $("code").value.length + " / " + MAX_CODE; });
  $("manual").addEventListener("change", applyManual);
  $("load").addEventListener("click", () => loadInto(slugify($("title").value)));
  $("refresh").addEventListener("click", async () => {
    if (!$("key").value) return toast("warning", "App key needed", "Enter your app key first.");
    const ok = await unlockAndLoad(false);
    if (ok) toast("info", "Refreshed", problems.length + " problem" + (problems.length === 1 ? "" : "s") + " loaded.");
  });
  document.querySelectorAll("#seg button").forEach((b) => b.addEventListener("click", () => { filter = b.dataset.f; renderList(); }));

  $("f").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    const manual = $("manual").checked;
    if (!$("key").value) { toast("warning", "App key needed", "Enter your app key first."); return $("key").focus(); }
    if (!$("title").value.trim()) { toast("warning", "Title needed", "Enter the problem title."); return $("title").focus(); }
    if (!manual && !$("code").value.trim()) { toast("warning", "Nothing to commit", "Paste your solution, or switch on Manual entry."); return $("code").focus(); }
    if ($("code").value.length > MAX_CODE) return toast("error", "Code too long", "Maximum is " + MAX_CODE + " characters.");

    if ($("sched").checked) return submitScheduled(manual);

    setBusy(true);
    try {
      const r = await api("/submit", { method: "POST", body: JSON.stringify({
        title: $("title").value, code: $("code").value, difficulty: $("difficulty").value,
        url: $("url").value, note: $("note").value, solved: $("solved").checked, manual: manual
      })});
      if (r.networkError || !r.res.ok) return failToast(r);
      const d = r.data, entry = d.problem && d.problem.manual;
      const tag = d.problem ? (d.problem.solved ? " (solved)" : " (unsolved)") : "";
      if (d.status === "created") toast("success", entry ? "Manual entry added" : "Solution committed", "Created " + d.path + tag, d.commit_url);
      else if (d.status === "updated") toast("success", entry ? "Entry updated" : "Solution updated", "Updated " + d.path + tag + ". Same file, new version.", d.commit_url);
      else toast("info", "No changes", "This is already committed exactly as entered.");
      (d.warnings || []).forEach((w) => toast("warning", "Heads up", w));
      if (d.status !== "unchanged") bumpActivity();
      if (d.problem) upsert(d.problem);
      $("note").value = "";
    } finally { setBusy(false); }
  });

  $("sched").addEventListener("change", applySched);
  $("when").addEventListener("input", updateWhen);
  document.querySelectorAll("#sched-panel [data-p]").forEach((b) => b.addEventListener("click", () => {
    $("when").value = toInput(presetDate(b.dataset.p)); updateWhen();
  }));

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") $("f").requestSubmit();
  });

  render();
})();
</script>
</body>
</html>
"""
