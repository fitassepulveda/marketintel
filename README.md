# Healthcare Market Intelligence Platform

AI-driven daily executive briefing covering six intelligence areas: National Healthcare
Policy & Industry, South Florida Competitive Intel, Payer & Insurance, Innovation & AI,
Public Health & Geopolitical Risk, and Reputation & Media Monitoring.

**Pipeline:** RSS + Yutori ingestion → SQLite store with dedup → composite scoring
(source weight × category weight × LLM relevance) → LLM synthesis → HTML email.

## Setup (5 minutes)

```bash
git clone <repo-url> && cd market-intel-platform
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # then fill in your keys (see below)
```

Required keys in `.env`:

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `YUTORI_API_KEY` | Yutori account (optional until procured — use `--no-yutori`) |
| `SMTP_USER` / `SMTP_PASS` | Gmail address + App Password (Google Account → Security → 2-Step Verification → App passwords) |
| `EMAIL_TO` | comma-separated recipients |

## Running

```bash
# 1. Verify the RSS feed URLs (do this first — some are best guesses)
python scripts/verify_sources.py

# 2. Test ingestion only (no API keys needed beyond nothing)
python run_briefing.py --dry-run --no-yutori --no-llm

# 3. Full dry run (needs ANTHROPIC_API_KEY): saves briefing to data/briefings/, doesn't email
python run_briefing.py --dry-run --no-yutori

# 4. Real run
python run_briefing.py
```

## Scheduling the daily run

**Option A — GitHub Actions (recommended):** `.github/workflows/daily-briefing.yml` runs the
pipeline in the cloud every weekday morning. Add the `.env` values as repository secrets
(Settings → Secrets and variables → Actions). No computer needs to be on.

**Option B — local cron (macOS/Linux):**
```
0 7 * * 1-5 cd /path/to/market-intel-platform && .venv/bin/python run_briefing.py
```

## Project status & roadmap

See `docs/Implementation_Plan.docx` for the full phased plan. Current state:

- [x] Repo scaffold, configs, pipeline skeleton
- [ ] 1.1 Verify all RSS feeds (`scripts/verify_sources.py`), fix URLs in `config/sources.yaml`
- [ ] 1.3 Replace the Yutori adapter stub (`src/ingest/yutori.py`) with the real API contract
- [ ] 2.6 Calibration: run daily with `--dry-run`, tune `config/weights.yaml` against feedback
- [ ] 3.5 Dry-run delivery to project team for ~5 business days
- [ ] 3.6 Go live to executive distribution list

## Tuning

All scoring behavior lives in `config/weights.yaml` (category/source weights, composite mix,
threshold, keyword boosts) and `config/settings.yaml` (org profile, key questions, models,
lookback window). No code changes needed to retune.

## Working on this repo with Claude Code

`CLAUDE.md` gives Claude Code full project context. Open a terminal in the repo and run
`claude`, then ask for a task, e.g. "implement task 1.3 — integrate the real Yutori API".
