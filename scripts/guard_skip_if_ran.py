"""Guard: skip the briefing if one already SUCCEEDED today.

The briefing now has two morning triggers for reliability — a reliable external
cron (cron-job.org → workflow_dispatch) plus GitHub's own (flaky) `schedule:` cron
kept as a backup. If both happen to fire on the same day we must not send the
digest twice (the second run would otherwise hit the 24h re-brief suppression and
mail a confusing "quiet-day" note). This guard asks the GitHub API whether the
"Daily Market Intelligence Briefing" workflow already has a SUCCESSFUL run today
(America/New_York) and, if so, tells the workflow to skip the send.

The currently-executing run is in_progress (not yet a success), so it never
matches itself — only a *prior* successful run today causes a skip.

Writes `should_run=true|false` to $GITHUB_OUTPUT. Fails OPEN: if the API can't be
reached, it returns should_run=true so a transient API hiccup never suppresses a
real briefing. Stdlib only (urllib), no pip install needed.

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


def _set_output(should_run: bool) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    line = f"should_run={'true' if should_run else 'false'}"
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    print(f"guard: {line}")


def _already_succeeded_today() -> bool:
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
    for run in data.get("workflow_runs", []):
        if str(run.get("id")) == str(this_run):
            continue  # never count ourselves
        created = run.get("created_at", "")  # ISO8601 UTC
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.astimezone(TZ).date() == today and run.get("conclusion") == "success":
            return True
    return False


def main() -> None:
    try:
        ran = _already_succeeded_today()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError) as e:
        # Fail OPEN: a transient API problem must never silence a real briefing.
        print(f"guard: could not verify prior runs ({e}); proceeding", file=sys.stderr)
        _set_output(True)
        return
    _set_output(not ran)


if __name__ == "__main__":
    main()
