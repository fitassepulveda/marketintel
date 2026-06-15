#!/usr/bin/env python3
"""One-time setup: create Yutori Scouts for selected non-RSS sources.

A Scout is a persistent web monitor. This script creates one per chosen source
(default: the two South Florida competitors we're piloting) and records the
returned scout IDs in the DB so the daily pipeline can poll them for updates.

Usage:
  python scripts/setup_scouts.py                 # create the default pilot scouts
  python scripts/setup_scouts.py --list          # list scouts already on the account
  python scripts/setup_scouts.py --sources "Baptist Health South Florida,Jackson Health System"
  python scripts/setup_scouts.py --dry-run       # show what would be created, create nothing

Idempotent: if a source already has a scout recorded in the DB, it is skipped
unless --force is passed.
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import config, store  # noqa: E402
from src.ingest import yutori  # noqa: E402

# Sources we're piloting the Scouting API on (must match names in sources.yaml).
DEFAULT_SOURCES = ["Baptist Health South Florida", "Jackson Health System"]

# Structured output we ask each scout to return — slots straight into the pipeline.
OUTPUT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "Short headline of the development"},
            "summary": {"type": "string", "description": "2-3 sentence summary of what happened"},
            "source_url": {"type": "string", "description": "URL of the primary source article"},
            "published_date": {"type": "string", "description": "Publication date of the article in YYYY-MM-DD format, if available on the page"},
        },
        "required": ["headline", "summary", "source_url"],
    },
}


def build_query(source: dict) -> str:
    """Competitor-monitoring query tuned to the briefing's SF-competitive factors."""
    return (
        f"Monitor news, press releases, and announcements about {source['name']}, "
        f"a South Florida health system (newsroom: {source['url']}). "
        "Report any developments involving: capital investment or new construction; "
        "capacity expansion (new hospitals, beds, facilities, or locations); "
        "new or expanded service lines and clinical programs; partnerships, mergers, "
        "or acquisitions; major executive or leadership changes; and notable awards, "
        "rankings, or reputational events. Focus on material competitive developments, "
        "not routine community PR. For each item, also capture the article's publication "
        "date from the page (the dateline or byline date) as published_date in YYYY-MM-DD."
    )


def find_source(cfg: dict, name: str):
    for area, sources in cfg["sources"].items():
        for s in sources:
            if s["name"] == name and s.get("type") == "yutori":
                return s, area
    return None, None


def list_scouts(base_url: str, headers: dict):
    resp = requests.get(base_url.rstrip("/") + "/scouting/tasks", headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    print(f"Total scouts on account: {data.get('total', 0)}  summary={data.get('summary')}")
    for sc in data.get("scouts", []):
        print(f"  [{sc['status']}] {sc['display_name']}  id={sc['id']}  "
              f"every {sc['output_interval']}s")


def next_run_timestamp(tz_name: str, hour: int) -> int:
    """Unix timestamp of the next HH:00 in tz_name (always in the future).

    Setting this as the scout's start_timestamp means the first scan — and, with a
    daily interval, every scan after — lands at `hour` local time. A future start also
    means no scan (and no charge) happens until then.
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone(timedelta(hours=-4))  # fallback: US Eastern (EDT)
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return int(target.timestamp())


def create_scout(base_url: str, headers: dict, query: str, output_interval: int,
                 user_timezone: str, start_timestamp: int = 0) -> dict:
    payload = {
        "query": query,
        "output_schema": OUTPUT_SCHEMA,
        "output_interval": output_interval,
        "user_timezone": user_timezone,
        "start_timestamp": start_timestamp,   # first run scheduled for the 6am slot
        "skip_email": True,          # we poll Get Updates; no Yutori emails
    }
    resp = requests.post(base_url.rstrip("/") + "/scouting/tasks",
                         headers={**headers, "Content-Type": "application/json"},
                         json=payload, timeout=120)
    if resp.status_code != 200:
        raise SystemExit(f"Scout creation failed ({resp.status_code}): {resp.text}")
    return resp.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", help="comma-separated source names (defaults to the pilot pair)")
    ap.add_argument("--list", action="store_true", help="list existing scouts and exit")
    ap.add_argument("--dry-run", action="store_true", help="print plan, create nothing")
    ap.add_argument("--force", action="store_true", help="recreate even if already recorded")
    ap.add_argument("--stop", action="store_true", help="archive recorded scouts (stop them running)")
    ap.add_argument("--restart", action="store_true", help="restart previously archived scouts")
    args = ap.parse_args()

    cfg = config.load_all()
    y = cfg["settings"]["yutori"]
    base_url = y["base_url"]
    headers = {"X-API-Key": config.env("YUTORI_API_KEY")}
    tz = cfg["settings"].get("org", {}).get("timezone", "America/New_York")
    interval = int(y.get("output_interval_seconds", 86400))
    scan_hour = int(cfg["settings"]["briefing"].get("scout_scan_hour_local", 6))
    start_ts = next_run_timestamp(tz, scan_hour)

    if args.list:
        list_scouts(base_url, headers)
        return

    names = [n.strip() for n in args.sources.split(",")] if args.sources else DEFAULT_SOURCES
    con = store.connect()

    if args.stop or args.restart:
        for name in names:
            if store.get_scout(con, name) is None:
                print(f"SKIP  '{name}': no scout recorded")
                continue
            if args.stop:
                yutori.stop_scout(con, y, name)
                print(f"STOP  '{name}': archived (will not run again)")
            else:
                yutori.restart_scout(con, y, name)
                print(f"START '{name}': restarted")
        con.commit()
        con.close()
        return

    for name in names:
        source, area = find_source(cfg, name)
        if source is None:
            print(f"SKIP  '{name}': not a yutori-type source in sources.yaml")
            continue
        if store.get_scout(con, name) and not args.force:
            existing = store.get_scout(con, name)
            print(f"SKIP  '{name}': already has scout {existing['scout_id']} (use --force to recreate)")
            continue

        query = build_query(source)
        first_run = datetime.fromtimestamp(start_ts, tz=timezone.utc).astimezone()
        if args.dry_run:
            print(f"DRY   would create scout for '{name}' [{area}] every {interval}s, "
                  f"first run ~{scan_hour}:00 {tz} (ts={start_ts})")
            print(f"      query: {query}")
            continue

        result = create_scout(base_url, headers, query, interval, tz, start_ts)
        store.upsert_scout(con, name, area, result["id"], query)
        con.commit()
        print(f"OK    '{name}' -> scout {result['id']}  (next output: {result.get('next_output_timestamp')})")

    con.close()


if __name__ == "__main__":
    main()
