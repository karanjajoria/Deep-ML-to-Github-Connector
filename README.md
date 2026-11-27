# deepml-sync

Commit Deep-ML solutions to GitHub from a GitHub-styled web page or an API call.
Re-committing the same problem updates its existing file (one file per problem, history in git).

## Two ways to connect
- **Use my GitHub** (anyone): a visitor pastes their own fine-grained token and repository in the *Connect GitHub* card. No app key needed. Their token is kept only in their browser (optionally for the tab session), is sent to GitHub through the app on each request, and is never stored or logged by the server. Scheduling is not available in this mode.
- **Owner access** (you): the app key unlocks your own configured repo (`GITHUB_TOKEN`/`GITHUB_REPO`), including scheduling. Your choice of mode is remembered in the browser.
- Don't want other people using your copy? Set `ALLOW_PERSONAL_TOKENS=0` in Render and only Owner access will work.
- Visitors are told in the manual to connect only on copies of the app run by someone they trust. Because their token passes through your server in memory to reach GitHub, that trust is real: don't add request logging that records headers.

## User manual
`/manual` is a public, plain-language guide for end users (create a repo, create a token, connect, every control, notifications, troubleshooting, staying safe). It is linked from the page header and the Connect card.

## Features
- **Solved toggle**: mark a problem solved or unsolved when committing, or flip it later from the problem list (makes a small "Mark solved/unsolved" commit).
- **Manual entry**: log a problem with no code (e.g. one you're still working on). Add the code later by committing the same title again. A manual update never overwrites code you already saved.
- **Your problems** list: newest first, filter All / Solved / Unsolved, click a title to load it into the form, link to the file on GitHub.
- **Stats** panel: solved / total, completion %, unsolved, manual entries, current and longest streak, a per-difficulty progress breakdown, and a GitHub-style activity heatmap (last 12 months). Streaks and the heatmap use your browser's timezone. They come from the commit history of `solutions/` (latest 500 commits, cached for 60 s); README-only commits aren't counted.
- **Scheduled commits**: turn on "Schedule for later", pick a date/time (shortcuts for Saturday, Sunday, tomorrow) and the solution is queued. A GitHub Actions timer publishes it when due. The *Scheduled* list lets you **Publish now** or **Cancel**.
- Header: the GitHub logo is the home button (your GitHub profile); "Deep-ML / Sync" opens your solutions repo.

## Setup
1. Create a repo (e.g. `deep-ml-solutions`) with a README so `main` exists.
2. GitHub > Settings > Developer settings > Fine-grained tokens: limit to that repo, **Contents: Read and write**.
3. Push this folder to its own GitHub repo, then on Render: New > Blueprint / Web Service.
4. Environment variables:
   - `GITHUB_TOKEN`  fine-grained token
   - `GITHUB_REPO`   `karanjajoria/deep-ml-solutions`
   - `APP_KEY`       **16+ random characters**. Generate one: `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`
   - `LINKEDIN_URL`, `CREDLY_URL`  your full profile links (https://...) for the footer
   - `GITHUB_BRANCH` (default `main`), `GITHUB_URL` (default your GitHub profile)
   - `REPO_URL` (optional) overrides where "Deep-ML / Sync" links; defaults to `https://github.com/<GITHUB_REPO>`
5. Verify your GitHub account email so commits show on your contribution graph.

## Security
- HTTPS enforced (HTTP redirects), HSTS, strict CSP with per-request nonce, no cookies, no framing, no-store caching.
- App key is compared in constant time; 5 wrong keys locks an IP for 15 min; 60 requests/min/IP limit.
- Same-origin check, request size cap, strict server-side validation (title, URL must be deep-ml.com, difficulty, size).
- GitHub token only in Render env vars, never sent to the browser or logged. Errors shown to you never include raw GitHub responses.
- Your code is passed straight to GitHub and not stored on the server.
- Limits: the rate limiter lives in memory (resets on restart/sleep) and the app key is a single shared secret. Rotate both the key and token if you ever suspect a leak.

## API
curl -X POST https://YOUR-APP.onrender.com/submit \
  -H "X-API-Key: $APP_KEY" -H "Content-Type: application/json" \
  -d '{"title":"Single Neuron","difficulty":"easy","solved":true,"code":"def f(): ...","note":"optional commit message"}'

# read-only: GET /api/problems, GET /api/activity (same X-API-Key header)

# manual entry (no code) and status toggle
curl -X POST .../submit -H ... -d '{"title":"Sigmoid","manual":true,"solved":false}'
curl -X POST .../api/status -H ... -d '{"slug":"sigmoid","solved":true}'


## Scheduled commits: one-time setup
How it works: queued items are saved as small JSON files on a separate branch (`deepml-queue`, created automatically). Commits on a non-default branch don't count on your contribution graph, so nothing shows up when you *schedule*. A GitHub Actions workflow calls `POST /api/run-due` on your app every 15 minutes; the app publishes anything due to `main` as a real commit (dated when it is published, never backdated), then deletes the queue file.

1. Make sure `.github/workflows/publish-scheduled.yml` is at the **root of your repo** on the default branch (scheduled workflows only run from there).
2. Repo > Settings > Secrets and variables > Actions > New repository secret:
   - `DEEPML_APP_URL`  `https://deep-ml-to-github-connector.onrender.com` (no trailing slash)
   - `DEEPML_APP_KEY`  the same value as your `APP_KEY` on Render
3. In Render, set the start command to `gunicorn app:app --workers 1 --threads 4 --timeout 120` (already in `render.yaml`; edit it by hand if you created the service manually).
4. Test it: schedule something a couple of minutes ahead, then Actions tab > "Publish scheduled solutions" > Run workflow.
5. Optional env var: `QUEUE_BRANCH` (default `deepml-queue`).

Things to know
- Timing is approximate: GitHub's scheduler can run a few minutes late, so expect publishing within roughly 15-30 minutes after the chosen time.
- If the repo is **public**, scheduled code is visible on the `deepml-queue` branch before it's published.
- Private repos: Actions minutes are limited (2,000/month free). Change the cron to hourly (`"7 * * * *"`) to stay under it.
- GitHub pauses scheduled workflows after 60 days without repo activity; your regular commits keep it alive.
- At most 30 pending items, at most 60 days ahead, at most 5 published per run (extras go out on the next run).
- Each timer call wakes the Render app, so on the free tier it will stay awake most of the time (free instance hours are shared across your workspace).


## Email notifications for scheduled publishes
When the timer publishes something, the workflow opens a GitHub issue assigned to (and mentioning) you, labelled `scheduled-publish`. GitHub emails you about it. Runs where nothing was published create nothing. The issue lists each solution with a link to the file and commit, plus any problems.
- Check https://github.com/settings/notifications: email should be on for "Participating, @mentions and custom" (the default) and your notification email address should be the one you read.
- Issues must be enabled on the repo (Settings > General > Features).
- If the workflow itself fails (wrong key, app down), GitHub emails you its standard failed-workflow notice (Settings > Notifications > Actions > "Failed workflows only").
- "Publish now" from the web page does not email you, since you're already there.
- Close or bulk-close the `scheduled-publish` issues whenever you like; they double as a publish log.
