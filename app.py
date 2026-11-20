"""Deep-ML -> GitHub sync service.

Open the page (or POST to /submit), give a problem title + your code, and it
commits solutions/<slug>.py to your repo and updates the README index.
Commits are made through the GitHub API with your token, so they count on
your contribution graph.
"""
import base64
import hmac
import os
import re
from datetime import date

import requests
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

TOKEN = os.environ["GITHUB_TOKEN"]          # fine-grained PAT, Contents: read/write on ONE repo
REPO = os.environ["GITHUB_REPO"]            # e.g. karanjajoria/deep-ml-solutions
BRANCH = os.environ.get("GITHUB_BRANCH", "main")
APP_KEY = os.environ["APP_KEY"]             # secret you choose; protects this service

API = f"https://api.github.com/repos/{REPO}/contents"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
}

FORM = """
<!doctype html><title>Deep-ML sync</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:system-ui;max-width:720px;margin:2rem auto;padding:0 1rem}
input,select,textarea,button{width:100%;margin:.4rem 0;padding:.6rem;font:inherit;box-sizing:border-box}
textarea{font-family:monospace;height:320px}</style>
<h2>Push Deep-ML solution to GitHub</h2>
<form method="post" action="/">
  <input name="key" type="password" placeholder="App key" required>
  <input name="title" placeholder="Problem title (e.g. Single Neuron)" required>
  <select name="difficulty"><option>easy</option><option>medium</option><option>hard</option></select>
  <input name="url" placeholder="Deep-ML problem URL (optional)">
  <textarea name="code" placeholder="Paste your solution" required></textarea>
  <button>Commit</button>
</form>
{% if msg %}<p><b>{{ msg }}</b></p>{% endif %}
"""


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "solution"


def get_file(path):
    r = requests.get(f"{API}/{path}", headers=HEADERS, params={"ref": BRANCH}, timeout=20)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    return base64.b64decode(data["content"]).decode(), data["sha"]


def put_file(path, content, message, sha=None):
    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(f"{API}/{path}", headers=HEADERS, json=body, timeout=20)
    r.raise_for_status()


def sync(title, code, difficulty="", url=""):
    slug = slugify(title)
    path = f"solutions/{slug}.py"

    header = f'"""{title}'
    if difficulty:
        header += f" ({difficulty})"
    if url:
        header += f"\n{url}"
    header += '\n"""\n\n'
    content = header + code.rstrip() + "\n"

    old, sha = get_file(path)
    if old == content:
        return "No changes: this solution is already committed."
    verb = "Update" if sha else "Solve"
    put_file(path, content, f"{verb}: {title}", sha)

    # Keep a README index
    readme, rsha = get_file("README.md")
    readme = readme or "# Deep-ML Solutions\n\n## Index\n"
    entry = f"- [{title}](solutions/{slug}.py)"
    if difficulty:
        entry += f" ({difficulty})"
    if url:
        entry += f" - [problem]({url})"
    if f"(solutions/{slug}.py)" not in readme:
        readme = readme.rstrip() + "\n" + entry + "\n"
        put_file("README.md", readme, f"Index: {title} ({date.today()})", rsha)
    return f"{verb}d {path}"


def authorized(key):
    return bool(key) and hmac.compare_digest(key, APP_KEY)


@app.route("/", methods=["GET", "POST"])
def home():
    msg = None
    if request.method == "POST":
        f = request.form
        if not authorized(f.get("key", "")):
            msg = "Wrong app key."
        else:
            try:
                msg = sync(f["title"], f["code"], f.get("difficulty", ""), f.get("url", ""))
            except requests.HTTPError as e:
                msg = f"GitHub error: {e.response.status_code} {e.response.text[:200]}"
    return render_template_string(FORM, msg=msg)


@app.route("/submit", methods=["POST"])
def submit():
    if not authorized(request.headers.get("X-API-Key", "")):
        return jsonify(error="unauthorized"), 401
    d = request.get_json(silent=True) or {}
    if not d.get("title") or not d.get("code"):
        return jsonify(error="title and code are required"), 400
    try:
        result = sync(d["title"], d["code"], d.get("difficulty", ""), d.get("url", ""))
    except requests.HTTPError as e:
        return jsonify(error="github", status=e.response.status_code), 502
    return jsonify(result=result)


@app.route("/healthz")
def health():
    return "ok"
