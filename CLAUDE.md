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
- `src/llm_client.py` — provider-agnostic LLM wrapper (`gemini` free tier or `anthropic`;
  set `llm.provider` in settings.yaml). Gemini calls are rate-paced for the free tier.
- `src/store.py` — SQLite schema, dedup, source-health log
- `src/ingest/rss.py` — feedparser-based ingestion (works today)
- `src/ingest/yutori.py` — Yutori **Scouting API** adapter. Scouts are persistent
  monitors created once per source (via `scripts/setup_scouts.py`); each run polls
  `GET /scouting/tasks/{id}/updates` for findings newer than the stored cursor and
  normalizes them. Source->scout_id mapping + cursor live in the `scouts` table.
  Auth is `X-API-Key`. Currently piloting 2 SF competitor sources (Baptist, Jackson).
- `scripts/setup_scouts.py` — create/manage Scouts for chosen sources
  (`--list`, `--dry-run`, `--force`, `--sources`, `--stop`, `--restart`). Must run
  where the network can reach api.yutori.com (i.e. locally, not in the sandbox).
- One-shot mode (`yutori.stop_after_first_update: true`, default): the briefing
  archives a scout (`POST /done`) as soon as it ingests that scout's first results,
  so each scout runs/bills exactly once. Set false for continuous daily monitoring.
  `run_briefing.py` only POLLS scouts (free); it never triggers a scout run.
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
- **Prioritization is pure LLM relevance, no keywords.** Every article published in
  the lookback window (settings `lookback_hours`, default 72 = past 3 days, filtered
  by PUBLISH date) is LLM-scored for relevance to UHealth against its area's key
  question. composite = `category_weight`·0.3 + `llm_relevance`·0.7 (source weight 0;
  area kept deliberately light). Items below `score_threshold` drop; top
  `briefing.max_stories` kept. The old keyword machinery (boost_keywords,
  critical_triggers, pre_rank, must-include) was removed — pool selection caps by
  recency only.
- **Dedup is semantic** (`dedup_cosine_similarity`): candidates are embedded
  (`gemini-embedding-001`) and clustered by cosine similarity. Keyword title/overlap
  rules remain only as a fallback if the embedding call fails.
- Secrets only in `.env` (local) or GitHub Actions secrets (CI). Never commit them.

## Current to-dos (see README roadmap + docs/Implementation_Plan.docx)

1. **Task 1.1** — run `scripts/verify_sources.py`; fix every FAIL url in sources.yaml
   (feed URLs marked `verify: true` are best guesses).
2. **Task 1.3** — Yutori Scouting API integrated (piloting Baptist + Jackson).
   To go live: run `python scripts/setup_scouts.py` locally to create the scouts,
   then extend to the other competitor sources. Confirm Scouting API pricing first.
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
