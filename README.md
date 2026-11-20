# deepml-sync
 
Commit Deep-ML solutions to GitHub from a GitHub-styled web page or an API call.
Re-committing the same problem updates its existing file (one file per problem, history in git).
 
## Features
- **Solved toggle**: mark a problem solved or unsolved when committing, or flip it later from the problem list (makes a small "Mark solved/unsolved" commit).
- **Manual entry**: log a problem with no code (e.g. one you're still working on). Add the code later by committing the same title again. A manual update never overwrites code you already saved.
- **Your problems** list: newest first, filter All / Solved / Unsolved, click a title to load it into the form, link to the file on GitHub.
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
 
# manual entry (no code) and status toggle
curl -X POST .../submit -H ... -d '{"title":"Sigmoid","manual":true,"solved":false}'
curl -X POST .../api/status -H ... -d '{"slug":"sigmoid","solved":true}'
 
- [Matrix-Vector Dot Product](solutions/matrix_vector_dot_product.py) · easy · unsolved · [problem](https://www.deep-ml.com/problems/1)
- [Single Neuron](solutions/single_neuron.py) · easy · solved · [problem](https://www.deep-ml.com/courses/neural-nets-to-gpt/learn?episode=ep1)
