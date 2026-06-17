# Reliable cloud scheduling for the daily briefing

## Why this exists

The briefing ran fine whenever it was triggered, but **GitHub's `schedule:` cron
silently stopped firing** — the Actions tab showed no run on 2026-06-16 or
2026-06-17, with the last run on 2026-06-15. This is a well-known GitHub
limitation: scheduled workflows are delayed and outright dropped, especially on
free runners. The watchdog couldn't help because it runs on the *same* unreliable
cron (so it never fired either) and its alerts went to the bot's own inbox.

**Fix:** stop depending on GitHub's scheduler. A reliable external cron service
triggers the workflow via the API each morning. GitHub's own cron stays on as a
free backup, and a same-day guard prevents a double-send if both ever fire.

```
cron-job.org (reliable)  ──POST workflow_dispatch──►  Daily Briefing workflow ──► email
GitHub schedule: (backup) ─────────────────────────►  (guard skips it if already sent)
cron-job.org (reliable)  ──POST workflow_dispatch──►  Watchdog workflow ──► alerts YOU
```

Already done in the repo (just needs to be pushed):
- `daily-briefing.yml`: `workflow_dispatch` is the primary path; `actions: read`
  permission, a `concurrency` group, and a guard step were added.
- `scripts/guard_skip_if_ran.py`: skips the send if a briefing already succeeded
  today (fails open, so it never suppresses a real run).

---

## One-time setup (≈15 min)

### 0. Push the code changes
From the repo on your Mac:
```bash
git add .github/workflows/daily-briefing.yml scripts/guard_skip_if_ran.py CLOUD_SCHEDULING.md
git commit -m "Reliable external-cron trigger + same-day guard"
git push
```

### 1. Create a fine-grained Personal Access Token
GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token.
- **Resource owner / Repository access:** only `fitassepulveda/marketintel`.
- **Repository permissions → Actions: Read and write.** (Nothing else.)
- **Expiration:** pick a date and set a calendar reminder to rotate before it.
- Copy the token (`github_pat_…`). It only goes into cron-job.org (step 3).

> This token can trigger workflows in this one repo and nothing more. It is
> separate from the old `ghp_…` token embedded in the repo's git remote — that
> one should still be rotated (it's stored in plaintext).

### 2. Set the watchdog's alert recipient
Repo → Settings → **Secrets and variables → Actions → New repository secret**:
- Name: `ALERT_EMAIL_TO`
- Value: `wef28@miami.edu`

Without this, missed-run alerts go to the bot's own Gmail and you never see them.

### 3. Create the two triggers on cron-job.org
Sign in at https://cron-job.org (free). Create **two** cron jobs. cron-job.org
runs on a real timezone, so it handles EDT/EST automatically — no UTC math.

**Job A — Briefing**
- URL: `https://api.github.com/repos/fitassepulveda/marketintel/actions/workflows/daily-briefing.yml/dispatches`
- Method: **POST**
- Request headers:
  - `Authorization: Bearer github_pat_…` (your token from step 1)
  - `Accept: application/vnd.github+json`
  - `X-GitHub-Api-Version: 2022-11-28`
- Request body: `{"ref":"main"}`
- Schedule: **06:07**, days **Mon–Fri**, timezone **America/New_York**.
- Enable cron-job.org's "notify on failed execution" so you're emailed if the
  trigger call itself ever fails.

**Job B — Watchdog** (catches a failed/missed run and emails you)
- URL: `https://api.github.com/repos/fitassepulveda/marketintel/actions/workflows/briefing-watchdog.yml/dispatches`
- Method / headers / body: **same as Job A**.
- Schedule: **08:12**, days **Mon–Fri**, timezone **America/New_York**.

A successful dispatch returns **HTTP 204** with an empty body — that's normal.

### 4. Send today's briefing now
Don't wait for tomorrow: GitHub → Actions → **Daily Market Intelligence
Briefing → Run workflow** (branch `main`). Or test the new trigger by running
Job A in cron-job.org manually.

---

## How to verify it's working
- **Tomorrow morning:** a run appears in the Actions tab around 6:07 ET and you
  receive the digest.
- **DB commits resume:** each successful run pushes a
  `chore: update intel.db dedup memory [skip ci]` commit — a quick health check.
- **If a run is ever missed:** the watchdog emails an `[ALERT]` to `ALERT_EMAIL_TO`.

## Notes / alternatives
- Any reliable scheduler works (EasyCron, Cronitor, Google Cloud Scheduler) —
  same POST. cron-job.org is just the simplest free option.
- The GitHub `schedule:` cron is intentionally left in place as a no-cost backup;
  the guard makes a double-fire harmless.
- Separate, non-urgent: the workflow's actions (checkout@v4, setup-python@v5,
  upload-artifact@v4) run on Node 20, which GitHub removes from runners on
  **2026-09-16**. Bump them before then.
