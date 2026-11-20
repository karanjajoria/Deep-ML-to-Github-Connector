"""Deep-ML -> GitHub sync service (hardened).

Open the page, enter the problem title + your code, and it commits
solutions/<slug>.py to your repo. Re-committing the same problem updates the
same file (no copies) and the README index entry.
"""
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import date
from functools import wraps
from urllib.parse import urlparse

import requests
from flask import Flask, g, jsonify, redirect, render_template_string, request
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
if len(APP_KEY) < 16:
    raise RuntimeError("APP_KEY must be at least 16 characters (use a long random string)")

APP_KEY_HASH = hashlib.sha256(APP_KEY.encode()).digest()

GITHUB_URL = _https_url("GITHUB_URL", "https://github.com/karanjajoria")
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


def get_file(path):
    r = _gh("GET", path, params={"ref": BRANCH})
    if r.status_code == 404:
        return None, None
    if not r.ok:
        log.warning("GitHub GET %s -> %s", path, r.status_code)
        raise GitHubError(r.status_code)
    data = r.json()
    if not isinstance(data, dict) or "content" not in data:
        raise GitHubError(500)
    return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]


def put_file(path, content, message, sha=None):
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode(),
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = _gh("PUT", path, json=body)
    if not r.ok:
        log.warning("GitHub PUT %s -> %s", path, r.status_code)
        raise GitHubError(r.status_code)
    return r.json().get("commit", {}).get("html_url", "")


def list_solutions():
    r = _gh("GET", "solutions", params={"ref": BRANCH})
    if r.status_code == 404:
        return []
    if not r.ok:
        raise GitHubError(r.status_code)
    out = []
    for item in r.json():
        m = re.fullmatch(r"([a-z0-9_]{1,100})\.py", item.get("name", ""))
        if m and item.get("type") == "file":
            slug = m.group(1)
            out.append({"slug": slug, "title": slug.replace("_", " ").title()})
    return sorted(out, key=lambda p: p["slug"])


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


def clean_code(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Solution code is required.")
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


# --------------------------------------------------------------------------
# Core sync logic
# --------------------------------------------------------------------------
HEADER_RE = re.compile(r'\A"""(.*?)\n"""\n\n', re.S)


def build_content(title, code, difficulty, url):
    head = title + (f" ({difficulty})" if difficulty else "")
    if url:
        head += "\n" + url
    return f'"""{head}\n"""\n\n{code.rstrip()}\n'


def parse_content(text):
    m = HEADER_RE.match(text)
    if not m:
        return {"title": "", "code": text, "difficulty": "", "url": ""}
    lines = m.group(1).split("\n")
    d = re.search(r"\((easy|medium|hard)\)$", lines[0])
    return {
        "title": re.sub(r"\s*\((easy|medium|hard)\)$", "", lines[0]),
        "code": text[m.end():],
        "difficulty": d.group(1) if d else "",
        "url": lines[1] if len(lines) > 1 else "",
    }


def update_readme(title, slug, difficulty, url):
    entry = f"- [{title}](solutions/{slug}.py)"
    if difficulty:
        entry += f" · {difficulty}"
    if url:
        entry += f" · [problem]({url})"
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


def sync(title, code, difficulty, url, note):
    slug = slugify(title)  # same title -> same file, so re-commits never create copies
    path = f"solutions/{slug}.py"

    commit_url = ""
    status = "unchanged"
    for attempt in range(2):
        old, sha = get_file(path)
        if old:
            title = parse_content(old)["title"] or title  # keep the original display title
        content = build_content(title, code, difficulty, url)
        if old == content:
            status = "unchanged"
            break
        verb = "Update" if sha else "Solve"
        message = f"{verb}: {title}" + (f" - {note}" if note else "")
        try:
            commit_url = put_file(path, content, message, sha)
            status = "updated" if sha else "created"
            break
        except GitHubError as exc:
            if exc.status in (409, 422) and attempt == 0:
                continue  # file changed under us; re-read sha and retry once
            raise

    warnings = []
    if status != "unchanged":
        try:
            update_readme(title, slug, difficulty, url)
        except GitHubError:
            warnings.append("Solution saved, but the README index could not be updated.")
    log.info("sync %s -> %s", path, status)
    return {"status": status, "path": path, "commit_url": commit_url, "warnings": warnings}


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


def github_failure(exc):
    messages = {
        0: "Couldn't reach GitHub. Try again in a moment.",
        401: "GitHub rejected the token (expired or revoked). Generate a new one.",
        403: "GitHub denied access or hit a rate limit. Check the token's Contents permission.",
        404: "Repo or branch not found, or the token can't access it.",
        409: "The file changed on GitHub while saving. Try again.",
        422: "GitHub couldn't process the commit. Try again.",
    }
    return err(messages.get(exc.status, "GitHub returned an error."), 502)


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
        title = clean_title(data.get("title"))
        code = clean_code(data.get("code"))
        difficulty = clean_difficulty(data.get("difficulty"))
        url = clean_url(data.get("url"))
        note = clean_note(data.get("note"))
    except ValueError as exc:
        return err(str(exc), 400)
    try:
        result = sync(title, code, difficulty, url, note)
    except GitHubError as exc:
        return github_failure(exc)
    return jsonify(ok=True, **result)


@app.get("/api/problems")
@protected
def problems():
    try:
        return jsonify(ok=True, problems=list_solutions())
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
--btn:#21262d;--btn-h:#30363d;--shadow:0 8px 24px rgba(1,4,9,.6)}
@media (prefers-color-scheme:light){:root{--bg:#ffffff;--subtle:#f6f8fa;--inset:#f6f8fa;--border:#d0d7de;
--fg:#1f2328;--muted:#59636e;--accent:#0969da;--green:#1f883d;--green-h:#1a7f37;--red:#d1242f;--amber:#9a6700;
--blue:#0969da;--btn:#f6f8fa;--btn-h:#eaeef2;--shadow:0 8px 24px rgba(140,149,159,.35)}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0}
body{background:var(--bg);color:var(--fg);min-height:100vh;display:flex;flex-direction:column;
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.topbar{background:var(--subtle);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;align-items:center;gap:12px}
.topbar svg{width:32px;height:32px;fill:var(--fg)}
.topbar .name{font-size:16px;color:var(--muted)}.topbar .name b{color:var(--fg);font-weight:600}
main{flex:1;width:100%;max-width:800px;margin:0 auto;padding:32px 16px}
h1{font-size:24px;font-weight:600;margin:0 0 4px;padding-bottom:8px}
.lead{color:var(--muted);margin:0 0 24px}
.box{border:1px solid var(--border);border-radius:6px;background:var(--bg)}
.box-header{background:var(--subtle);border-bottom:1px solid var(--border);padding:12px 16px;font-weight:600;
display:flex;align-items:center;gap:8px;border-radius:6px 6px 0 0}
.box-header svg{width:16px;height:16px;fill:var(--muted)}
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
  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>
  <span class="name">Deep-ML <span>/</span> <b>Sync</b></span>
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

      <div class="field">
        <label for="code">Solution</label>
        <div class="editor">
          <div class="editor-head"><span>solution.py</span><span id="count">0 / {{ max_code }}</span></div>
          <textarea id="code" spellcheck="false" placeholder="Paste your solution here" required></textarea>
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
  let known = new Set(), lastKey = "", busy = false, hintTimer;

  const svg = (inner) => '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + inner + "</svg>";
  const ICONS = {
    success: svg('<circle cx="8" cy="8" r="6.5"/><path d="M5.2 8.2l2 2 3.6-4"/>'),
    error: svg('<circle cx="8" cy="8" r="6.5"/><path d="M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8"/>'),
    info: svg('<circle cx="8" cy="8" r="6.5"/><path d="M8 7.2v4M8 4.8v.01"/>'),
    warning: svg('<path d="M8 2L14.5 13.5H1.5Z"/><path d="M8 6.5v3.2M8 11.6v.01"/>')
  };

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
      res = await fetch(path, Object.assign({}, options, { headers, cache: "no-store", credentials: "omit" }));
    } catch (e) { return { networkError: true }; }
    let data = {};
    try { data = await res.json(); } catch (e) {}
    return { res, data };
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

  function updateHint() {
    const slug = slugify($("title").value);
    const hit = slug && known.has(slug);
    $("hint").hidden = !hit; $("load").hidden = !hit;
    if (hit) $("hint-text").textContent = "A solution for this problem already exists. Committing will update solutions/" + slug + ".py; no copy will be created.";
  }

  async function loadProblems() {
    const key = $("key").value;
    if (!key || key === lastKey) return;
    lastKey = key;
    const r = await api("/api/problems");
    if (r.networkError || !r.res.ok) { lastKey = ""; return failToast(r); }
    known = new Set(r.data.problems.map((p) => p.slug));
    const dl = $("problems"); dl.replaceChildren();
    r.data.problems.forEach((p) => { const o = document.createElement("option"); o.value = p.title; dl.appendChild(o); });
    toast("info", "Unlocked", r.data.problems.length + " saved solution" + (r.data.problems.length === 1 ? "" : "s") + " found.");
    updateHint();
  }

  function setBusy(on) {
    busy = on;
    const go = $("go");
    go.disabled = on;
    go.replaceChildren();
    if (on) { const s = document.createElement("span"); s.className = "spin"; go.append(s, "Committing\u2026"); }
    else go.textContent = "Commit changes";
  }

  $("key").addEventListener("change", loadProblems);
  $("title").addEventListener("input", () => { clearTimeout(hintTimer); hintTimer = setTimeout(updateHint, 150); });
  $("code").addEventListener("input", () => { $("count").textContent = $("code").value.length + " / " + MAX_CODE; });

  $("load").addEventListener("click", async () => {
    const slug = slugify($("title").value);
    const r = await api("/api/solution?slug=" + encodeURIComponent(slug));
    if (r.networkError || !r.res.ok) return failToast(r);
    $("code").value = r.data.code;
    if (r.data.difficulty) $("difficulty").value = r.data.difficulty;
    $("url").value = r.data.url || "";
    $("code").dispatchEvent(new Event("input"));
    toast("info", "Loaded", "Current code for this problem is in the editor.");
  });

  $("f").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    if (!$("key").value) { toast("warning", "App key needed", "Enter your app key first."); return $("key").focus(); }
    if (!$("title").value.trim()) { toast("warning", "Title needed", "Enter the problem title."); return $("title").focus(); }
    if (!$("code").value.trim()) { toast("warning", "Nothing to commit", "Paste your solution first."); return $("code").focus(); }
    if ($("code").value.length > MAX_CODE) return toast("error", "Code too long", "Maximum is " + MAX_CODE + " characters.");

    setBusy(true);
    try {
      const r = await api("/submit", { method: "POST", body: JSON.stringify({
        title: $("title").value, code: $("code").value, difficulty: $("difficulty").value,
        url: $("url").value, note: $("note").value
      })});
      if (r.networkError || !r.res.ok) return failToast(r);
      const d = r.data;
      if (d.status === "created") toast("success", "Solution committed", "Created " + d.path, d.commit_url);
      else if (d.status === "updated") toast("success", "Solution updated", "Updated " + d.path + " (same file, new version).", d.commit_url);
      else toast("info", "No changes", "This code is already committed for this problem.");
      (d.warnings || []).forEach((w) => toast("warning", "Heads up", w));
      if (d.status === "created") { known.add(slugify($("title").value)); updateHint(); }
      $("note").value = "";
    } finally { setBusy(false); }
  });

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") $("f").requestSubmit();
  });
})();
</script>
</body>
</html>
"""