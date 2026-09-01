#!/usr/bin/env python3
"""Send an already-rendered briefing HTML file to one address, using .env SMTP settings.

    python scripts/send_html.py data/briefings/2026-09-01_rafic_weiss_preview.html wef28@miami.edu \
        --subject "Market Intelligence Briefing — Rafic & Dr. Weiss preview"

Exists because the Cowork sandbox has no outbound SMTP: briefings can be BUILT there but
must be SENT from a networked terminal.
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import config
from src.output import emailer

ap = argparse.ArgumentParser()
ap.add_argument("html_file")
ap.add_argument("to")
ap.add_argument("--subject", default="Market Intelligence Briefing")
a = ap.parse_args()

body = Path(a.html_file).read_text(encoding="utf-8")
emailer.send(body, a.subject, {
    "host": config.env("SMTP_HOST"), "port": config.env("SMTP_PORT"),
    "user": config.env("SMTP_USER"), "password": config.env("SMTP_PASS"),
    "from": config.env("EMAIL_FROM"), "to": [a.to]}, subtype="html")
print(f"Sent {a.html_file} to {a.to}.")
