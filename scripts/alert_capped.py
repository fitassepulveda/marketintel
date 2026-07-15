"""Email an alert when the daily briefing halts after too many failed runs today.

Run by daily-briefing.yml when guard_skip_if_ran.py emits alert=true (i.e. the
failed-run cap was hit). Sends ONE email to the owner explaining that no briefing
went out and no further attempts will be made today. Stdlib only.

Env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_FROM,
     ALERT_EMAIL_TO (owner; falls back to EMAIL_TO), GITHUB_REPOSITORY.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def main() -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    sender = os.environ.get("EMAIL_FROM") or user
    to = os.environ.get("ALERT_EMAIL_TO") or os.environ["EMAIL_TO"]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    runs_url = f"https://github.com/{repo}/actions/workflows/daily-briefing.yml"

    msg = EmailMessage()
    msg["Subject"] = "ALERT: daily briefing HALTED — too many failed runs today"
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(
        "The Daily Market Intelligence Briefing hit its failed-run cap and will make "
        "NO further attempts today. No briefing was sent.\n\n"
        f"Run history / logs:\n  {runs_url}\n\n"
        "Most likely causes:\n"
        "  - Gemini API quota or billing problem (429s in the 'Run briefing pipeline' "
        "step; check the Google Cloud billing account linked to the API key's project)\n"
        "  - A code/config error introduced in a recent push (same error in every run)\n\n"
        "Once fixed, trigger a run manually from the Actions page (Run workflow) or "
        "wait for tomorrow's cron.\n"
    )
    with smtplib.SMTP(host, port, timeout=60) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"alert_capped: sent halt alert to {to}")


if __name__ == "__main__":
    main()
