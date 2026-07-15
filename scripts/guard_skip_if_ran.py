"""Guard: skip the briefing if one already SUCCEEDED today, or if too many
runs already FAILED today.

Two jobs:

1. Same-day duplicate suppression. The briefing has two morning triggers for
   reliability — a reliable external cron (cron-job.org → workflow_dispatch) plus
   GitHub's own (flaky) `schedule:` cron as a backup. If both fire on the same day
   we must not send the digest twice.

2. Failed-run cap. The external cron re-dispatches every 30 minutes, which is great
   for transient failures but disastrous for persistent ones (e.g. the 2026-07-15
   Gemini quota outage: six 29-58 minute runs hammering a dead API). After
   MAX_FAILED_RUNS_PER_DAY failures in one day we stop trying and emit alert=true
   exactly once, so the workflow emails the owner instead of retrying forever.
   Note: the run that skips-and-alerts itself concludes "success", which makes every
   later dispatch today skip via the success check — that is what makes the alert
   fire only once, with no extra state.

The currently-executing run is in_progress (not yet a success/failure), so it never
matches itself — only *prior* runs today are counted.

Writes `should_run=true|false` and `alert=true|false` to $GITHUB_OUTPUT. Fails OPEN:
if the API can't be reached, it returns should_run=true so a transient API hiccup
never suppresses a real briefing. Stdlib only (urllib), no pip install needed.

Env (provided by .github/workflows/daily-briefing.yml):
  GH_TOKEN           - token with actions:read (the Actions GITHUB_TOKEN)
  GITHUB_REPOSITORY  - "owner/repo" (auto-set by GitHub Actions)
  GITHUB_RUN_ID      - this run's id (auto-set); excluded from the check defensively
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

WORKFLOW_FILE = "daily-briefing.yml"
TZ = ZoneInfo("America/New_York")
# With quota-type failures now failing fast (~2-3 min) and the external cron
# dispatching every 30 min, 4 failures ≈ two hours of genuine attempts.
MAX_FAILED_RUNS_PER_DAY = 4


def _set_outputs(should_run: bool, alert: bool = False) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    lines = [
        f"should_run={'true' if should_run else 'false'}",
        f"alert={'true' if alert else 'false'}",
    ]
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print(f"guard: {' '.join(lines)}")


def _todays_outcomes() -> tuple[bool, int]:
    """Return (succeeded_today, failures_today) for prior runs of this workflow."""
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GH_TOKEN"]
    this_run = os.environ.get("GITHUB_RUN_ID", "")
    today = datetime.now(TZ).date()
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{WORKFLOW_FILE}/runs?per_page=30"
    )
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    succeeded, failures = False, 0
    for run in data.get("workflow_runs", []):
        if str(run.get("id")) == str(this_run):
            continue  # never count ourselves
        created = run.get("created_at", "")  # ISO8601 UTC
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.astimezone(TZ).date() != today:
            continue
        if run.get("conclusion") == "success":
            succeeded = True
        elif run.get("conclusion") == "failure":
            failures += 1
    return succeeded, failures


def main() -> None:
    try:
        succeeded, failures = _todays_outcomes()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError) as e:
        # Fail OPEN: a transient API problem must never silence a real briefing.
        print(f"guard: could not verify prior runs ({e}); proceeding", file=sys.stderr)
        _set_outputs(True)
        return
    if succeeded:
        _set_outputs(False)             # already sent today — normal duplicate guard
    elif failures >= MAX_FAILED_RUNS_PER_DAY:
        print(f"guard: {failures} failed runs today (cap {MAX_FAILED_RUNS_PER_DAY}); "
              "halting for the day and alerting", file=sys.stderr)
        _set_outputs(False, alert=True)  # this run then concludes 'success', so
        #                                  later dispatches today skip silently
    else:
        _set_outputs(True)


if __name__ == "__main__":
    main()
