# Project: Healthcare Market Intelligence Platform

Daily AI-driven market intelligence briefing for executives of a South Florida academic
health system. Monitors six intelligence areas, scores stories with a composite model,
synthesizes an executive summary, and emails it every weekday morning.

## Architecture

```
RSS feeds ──────────┐
                    ├─> SQLite (data/intel.db, dedup by URL hash)
Yutori scraping ────┘        │
                             v
              pre-rank (source+category+keywords, no LLM cost)
                             v
              LLM relevance scoring (Claude Haiku, batched, top N only)
                             v
              composite score = 0.2·source + 0.3·category + 0.5·LLM  (0-100)
                             v
              threshold filter -> top stories
                             v
              LLM synthesis (Claude Sonnet) -> exec summary JSON
                             v
              HTML email via SMTP (+ saved to data/briefings/)
```

Entry point: `run_briefing.py`. Flags: `--dry-run`, `--no-yutori`, `--no-llm`.

## Code map

- `src/config.py` — loads `config/*.yaml` + `.env`
- `src/store.py` — SQLite schema, dedup, source-health log
- `src/ingest/rss.py` — feedparser-based ingestion (works today)
- `src/ingest/yutori.py` — **STUB**: adapter for Yutori scraping/enrichment; replace
  `_call_yutori` with the real API contract once the key is procured. Keep the
  normalized output dict shape unchanged.
- `src/prioritize/scoring.py` — pre-rank + composite scoring (pure functions)
- `src/prioritize/llm_relevance.py` — batched 0-10 scoring vs each area's key question
- `src/output/synthesize.py` — briefing JSON generation
- `src/output/emailer.py` — HTML render + SMTP send
- `scripts/verify_sources.py` — checks every RSS URL in sources.yaml

## Config conventions (important)

- **Never hardcode** sources, weights, key questions, or org details in Python.
  They live in `config/sources.yaml`, `config/weights.yaml`, `config/settings.yaml`.
- Category priority weights come from the approved proposal deck and should not be
  changed without leadership sign-off: SF Competitive 10, National Policy 9, Payer 7,
  Innovation 5, Public Health 4, Reputation 3.
- SF competitive sub-factors: capital investment 10, capacity expansion 9,
  geographic proximity 8, service line impact 7 (handled in the LLM scoring prompt).
- Secrets only in `.env` (local) or GitHub Actions secrets (CI). Never commit them.

## Current to-dos (see README roadmap + docs/Implementation_Plan.docx)

1. **Task 1.1** — run `scripts/verify_sources.py`; fix every FAIL url in sources.yaml
   (feed URLs marked `verify: true` are best guesses).
2. **Task 1.3** — integrate real Yutori API in `src/ingest/yutori.py`.
3. **Task 2.6** — calibration: tune weights.yaml against ranked dry-run output.
4. **Task 3.5/3.6** — dry-run period, then go live.

## Style

- Python 3.11+, stdlib + the deps in requirements.txt only; no heavy frameworks.
- Keep modules small and pure where possible; pipeline state goes through SQLite.
- Cost discipline: LLM-score at most `max_llm_scored_items`; Yutori only for
  non-RSS sources; pre-filter with keywords before any paid call.
- Log per-source results every run; source failures must never crash the pipeline.

## Testing without spending money

`python run_briefing.py --dry-run --no-yutori --no-llm` exercises ingestion, dedup,
pre-ranking, threshold logic, HTML rendering, and file output with zero API calls.
