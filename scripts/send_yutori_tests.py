"""Email ALREADY-GENERATED Yutori A/B comparisons — no rerun, no Yutori cost.

Reads data/briefings/_yutori_test_<date>_compare.html for each date given and sends
them stacked in ONE email.

Usage (venv active; SMTP_* in .env):
  python3 scripts/send_yutori_tests.py 2026-07-10 2026-07-13
  python3 scripts/send_yutori_tests.py 2026-07-10 2026-07-13 --to francokique@gmail.com
  (default recipients: the feedback contacts)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config                    # noqa: E402
from src.output import emailer            # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dates", nargs="+", help="briefing dates, e.g. 2026-07-10 2026-07-13")
    ap.add_argument("--to", default=",".join(emailer.FEEDBACK_CONTACTS),
                    help="comma-separated recipients")
    args = ap.parse_args()

    config.load_all()  # loads .env
    out = config.DATA_DIR / "briefings"
    sections = []
    for d in args.dates:
        p = out / f"_yutori_test_{d}_compare.html"
        if not p.exists():
            sys.exit(f"Not found: {p} — run test_yutori_value.py --date {d} first.")
        sections.append(p.read_text(encoding="utf-8"))
        print(f"Attached comparison for {d}")

    body = '<hr style="margin:40px 0;border:none;border-top:3px solid #006888">'.join(sections)
    subject = f"[TEST] Yutori value test (A/B) — {', '.join(args.dates)}"

    if not all(config.env(k, required=False) for k in
               ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "EMAIL_FROM")):
        sys.exit("SMTP_* / EMAIL_FROM not set in .env — not sent.")
    recipients = [e.strip() for e in args.to.split(",") if e.strip()]
    emailer.send(
        body, subject,
        {"host": config.env("SMTP_HOST"), "port": config.env("SMTP_PORT"),
         "user": config.env("SMTP_USER"), "password": config.env("SMTP_PASS"),
         "from": config.env("EMAIL_FROM"), "to": recipients},
        subtype="html",
    )
    print(f"Sent to {', '.join(recipients)}")


if __name__ == "__main__":
    main()
