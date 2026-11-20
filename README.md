# deepml-sync

Commit Deep-ML solutions to GitHub from a web form or API call.

## Setup
1. Create an empty repo (e.g. `deep-ml-solutions`) with a `main` branch (tick "Add a README").
2. GitHub > Settings > Developer settings > Fine-grained tokens: create a token limited to that
   repo with **Contents: Read and write**.
3. Push this folder to a separate GitHub repo, then on Render: New > Blueprint (or Web Service)
   and point it at that repo.
4. Set env vars: `GITHUB_TOKEN`, `GITHUB_REPO` (`karanjajoria/deep-ml-solutions`), `APP_KEY` (any long random string).
5. Make sure the email on your GitHub account is verified so commits show on your graph.

## Use
- Open your Render URL, paste title + code, Commit.
- Or call it:
  curl -X POST https://YOUR-APP.onrender.com/submit -H "X-API-Key: $APP_KEY" -H "Content-Type: application/json" \
    -d '{"title":"Single Neuron","difficulty":"easy","code":"def f(): ..."}'

Render's free tier sleeps when idle, so the first request may take about 30 seconds.
