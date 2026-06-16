"""Watchdog: verify today's market-intelligence briefing actually ran.

Runs as its own scheduled GitHub Actions workflow a couple of hours after the
briefing. Asks the GitHub API whether the "Daily Market Intelligence Briefing"
workflow had a SUCCESSFUL run today (America/New_York). If not — whether the
scheduled trigger was silently dropped by GitHub, or the run failed — it emails
an alert so a missing briefing never goes unnoticed.

Stdlib only (urllib + smtplib), so no pip install is needed. Matches the SMTP
approach in src/output/emailer.py (SMTP + STARTTLS + login).

Env it expects (all provided by .github/workflows/briefing-watchdog.yml):
  GH_TOKEN            - token with actions:read on this repo (the Actions GITHUB_TOKEN)
  GITHUB_REPOSITORY   - "owner/repo" (auto-set by GitHub Actions)
  SMTP_HOST/PORT/USER/PASS, EMAIL_FROM   - same secrets the briefing uses
  ALERT_EMAIL_TO      - comma-separated alert recipients (falls back to SMTP_USER)
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

WORKFLOW_FILE = "daily-briefing.yml"
TZ = ZoneInfo("America/New_York")


def _env(key: str, required: bool = True, default: str = "") -> str:
    val = os.environ.get(key, default)
    if required and not val:
        print(f"watchdog: missing required env var {key}", file=sys.stderr)
        sys.exit(1)
    return val


def _briefing_succeeded_today() -> bool:
    """True if the briefing workflow has a successful run dated today (ET)."""
    repo = _env("GITHUB_REPOSITORY")
    token = _env("GH_TOKEN")
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
        created = run.get("created_at", "")  # ISO8601 UTC, e.g. 2026-06-16T11:17:05Z
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.astimezone(TZ).date() == today and run.get("conclusion") == "success":
            return True
    return False


def _send_alert(reason: str) -> None:
    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT", required=False, default="587") or "587")
    user = _env("SMTP_USER")
    password = _env("SMTP_PASS")
    sender = _env("EMAIL_FROM")
    to = _env("ALERT_EMAIL_TO", required=False) or user
    recipients = [a.strip() for a in to.split(",") if a.strip()]
    today_str = datetime.now(TZ).strftime("%A, %B %d, %Y")

    body = (
        f"No successful Market Intelligence briefing run was found for {today_str}.\n\n"
        f"Reason: {reason}\n\n"
        "This usually means GitHub silently dropped the scheduled run, or the run "
        "failed. To send today's briefing now:\n"
        "  - GitHub > Actions > 'Daily Market Intelligence Briefing' > Run workflow\n"
        "  - or locally: python3 run_briefing.py\n"
    )
    msg = MIMEText(body, "plain")
    msg["Subject"] = f"[ALERT] Market Intelligence briefing did NOT run - {today_str}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(sender, recipients, msg.as_string())
    print(f"watchdog: ALERT sent to {recipients} ({reason})")


def main() -> None:
    try:
        ok = _briefing_succeeded_today()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        # Never fail silently: if we can't verify, alert instead of assuming OK.
        _send_alert(f"watchdog could not query the GitHub API ({e})")
        return
    if ok:
        print("watchdog: briefing ran successfully today - all good.")
        return
    _send_alert("no successful briefing workflow run found for today")


if __name__ == "__main__":
    main()
