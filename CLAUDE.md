# Project: Healthcare Market Intelligence Platform

Daily AI-driven market-intelligence briefing for executives of **University of Miami
Health System (UHealth)**. It monitors six intelligence areas, scores every recent story
by LLM relevance to UHealth, removes duplicates semantically, writes an executive summary,
and emails an HTML digest of the top stories each weekday morning.

> This file is the catch-up doc for a fresh session. It reflects the **current** design
> (the project evolved a lot during build — older notes/diagrams elsewhere may be stale).
> `README.md` has a step-by-step "how it works & why." Read both before changing anything.

## Status (as of last session)

- **Live and automated.** Validated end-to-end; runs send real email.
- **Scheduling is external-cron-driven (changed 2026-06-17).** GitHub's own `schedule:`
  cron proved unreliable — it silently dropped the daily run on 2026-06-16 and 2026-06-17
  (Actions tab showed no run; last was 06-15). The **reliable trigger is now an external
  cron service (cron-job.org) calling the `workflow_dispatch` API** each weekday ~6:07am ET.
  GitHub's `schedule:` cron is kept only as a free backup; a **same-day guard**
  (`scripts/guard_skip_if_ran.py`) + a `concurrency` group ensure the briefing sends at
  most once if both fire. Full setup in `CLOUD_SCHEDULING.md`. Yutori scouts scan at 5am ET.
- **Provider:** Gemini, with **billing enabled** (no longer on the flaky free tier).
- **Scouts:** 2 active competitor scouts (Baptist Health, Jackson Health), scanning daily.
- **Recipients:** wef28@miami.edu (trimmed from three to one on 2026-06-16).
- All work committed and pushed to `github.com/fitassepulveda/marketintel` (branch `main`).

## Pipeline (current)

```
RSS / Google-News feeds ─┐
                         ├─> SQLite (data/intel.db, exact-URL dedup at write)
Yutori "scouts" ─────────┘        │
                                  v
        keep only items PUBLISHED in last lookback_hours (72h = 3 days; by publish
        date, fallback to fetch time when undated)
                                  v
        per-source cap (max_per_source, most-recent kept; competitors exempt)
                                  v
        LLM relevance scoring 0-10 vs each area's key question (Gemini, temp 0)
                                  v
        composite = source·0.0 + category·0.0 + llm·1.0  ->  currently 100% LLM relevance
        (weights live in weights.yaml; were 0.2/0.3/0.5 originally, tuned to pure LLM)
                                  v
        drop below score_threshold (55) -> sort -> SEMANTIC dedup (embeddings) -> top N
                                  v
        LLM synthesis (Gemini) -> per-story narrative JSON
                                  v
        HTML digest email via SMTP (+ files saved to data/briefings/)
        If nothing qualifies: a short "quiet-day" email is sent instead of silence.
```

Entry point: `run_briefing.py`. Flags: `--dry-run` (build + save, don't send),
`--no-yutori`, `--no-llm`.

## Code map

- `run_briefing.py` — orchestrates: `ingest()` → `prioritize()` → synthesis → digest/send.
  Key helpers: `_is_recent()` (publish-date window), `_parse_dt()`, `_send_html()`
  (dry-run/SMTP-guarded send), quiet-day branch when `prioritize()` returns nothing.
- `src/config.py` — loads `config/*.yaml` + `.env`. `config.env(key, required=)`.
- `src/llm_client.py` — Gemini/Anthropic wrapper. Gemini specifics: auth via
  **`x-goog-api-key` header** (the new `AQ.` key format does NOT work as `?key=`);
  **temperature 0** (deterministic scoring); retries on 429/500/503 with backoff;
  `thinkingConfig` only sent for 2.5 models. `embed()` calls `gemini-embedding-001`
  (`batchEmbedContents`) for semantic dedup.
- `src/store.py` — SQLite schema + helpers. Tables: `articles` (incl. `published`,
  `fetched`, `llm_score`, `composite_score`, `briefed_on`), `source_health`,
  `scouts` (source→scout_id, `last_update_ts` cursor, `active` flag).
- `src/ingest/rss.py` — feedparser ingestion; strips stray HTML from titles/summaries.
- `src/ingest/enrich.py` — shared best-effort publish-date extractor: fetches an article
  page and reads `article:published_time`/`og:`/JSON-LD `datePublished`/`<time>`. Used by
  both the RSS path (via `run_briefing._enrich_undated`) and Yutori scouts. Never raises.
- `src/ingest/yutori.py` — Yutori **Scouting API** adapter. Scouts are persistent
  monitors created once per source (via `scripts/setup_scouts.py`); each run polls
  `GET /scouting/tasks/{id}/updates` for findings newer than the stored cursor. Also
  enriches missing publish dates by fetching the article page metadata
  (`enrich_publish_dates`). Auth `X-API-Key`.
- `src/prioritize/scoring.py` — `composite()`, `semantic_dedupe()` (cosine over
  embeddings), `dedupe_by_title()` (keyword fallback), date/money/title helpers.
  (Old `pre_rank`/`keyword_hits`/`critical_match` were removed.)
- `src/prioritize/llm_relevance.py` — batched 0-10 scoring (batch_size 15, max_tokens
  4000 to avoid JSON truncation).
- `src/output/synthesize.py` — briefing JSON; one story per item (dedup is upstream).
- `src/output/emailer.py` — `render_digest` (plain text), `render_digest_html` (the
  sent email: area tag + source + larger title), `render_quiet_html`, `_norm_url`,
  `_fmt_date`, `send()`.
- `scripts/setup_scouts.py` — create/manage scouts: `--list`, `--dry-run`, `--force`
  (archives the old scout first to avoid orphan billing), `--sources`, `--stop`,
  `--restart`. Schedules first run at `scout_scan_hour_local` (5am) and asks for
  `published_date`. **Must run locally** (needs network to api.yutori.com).
- `scripts/verify_sources.py` — checks RSS URLs. `scripts/score_report.py` — debug ranking.
- `.github/workflows/daily-briefing.yml` — primary trigger is **`workflow_dispatch`**
  (called by the external cron service, the reliable path); a `schedule:` cron (`7 10`)
  remains as a best-effort backup. Has `actions: read` + a `concurrency` group, and a
  guard step that gates the real work on `should_run`.
- `scripts/guard_skip_if_ran.py` — same-day guard. Queries the GitHub API for a
  *successful* briefing run already today (ET); if found, emits `should_run=false` so a
  redundant trigger is a no-op (prevents a duplicate / quiet-day double-send). **Fails
  open** — an API error yields `should_run=true`, never suppressing a real run.
- `scripts/watchdog.py` + `.github/workflows/briefing-watchdog.yml` — missed-run
  backstop. Queries the GitHub API for a *successful* briefing run dated today (ET); if
  none, emails an `[ALERT]` via the SMTP secrets. Alert goes to `ALERT_EMAIL_TO`
  (set to `wef28@miami.edu`), falling back to `SMTP_USER`. Trigger it the same reliable
  way (external cron → `workflow_dispatch`, ~8:12am ET); its `schedule:` cron is backup only.
- `CLOUD_SCHEDULING.md` — the why + exact one-time setup (PAT, cron-job.org jobs, secret).

## Config (current values, all in `config/`)

`settings.yaml`
- `org.name` "University of Miami Health System", `org.short_name` "UM",
  `org.timezone` America/New_York.
- `llm.provider: gemini`; scoring & synthesis both `gemini-2.5-flash`.
- `briefing.lookback_hours: 72`, `rebrief_after_hours: 24`, `enrich_publish_dates: true`,
  `enrich_timeout_seconds: 10`, `max_stories: 5`, `digest_top_n: 5`,
  `digest_recipients: [wef28@miami.edu]`.
- `yutori`: `output_interval_seconds: 86400` (daily), `stop_after_first_update: false`
  (keep running daily), `scout_scan_hour_local: 5`, `enrich_publish_dates: true`.

`weights.yaml`
- `composite: source 0.0 / category 0.0 / llm 1.0` → **100% LLM relevance** (user's
  choice; tune here for area weighting). `score_threshold: 55`.
- `dedup_cosine_similarity: 0.85` (primary), `dedup_title_similarity: 0.90` +
  `dedup_token_overlap: 0.6` (fallback only).
- `max_per_source: 15`; `uncapped_sources:` the competitor feeds (exempt from the cap).
- `category_weights` (from the proposal deck, don't change without sign-off):
  SF Competitive 10, National Policy 9, Payer 7, Innovation 5, Public Health 4, Reputation 3.

`sources.yaml` — feeds per area. `type: rss` (free, headlines+links) or `type: yutori`
(scraped). Note: do NOT point a Yutori scout at paywalled sites (Modern Healthcare,
Becker's) — terms prohibit it; use headline RSS proxies instead.

## Design decisions worth remembering

- **Pure LLM relevance, no keywords.** Keyword gating/boosts were removed — they surfaced
  local fluff (e.g. "Miami" matching sports) and missed well-worded stories. The LLM judges
  relevance to UHealth; area weight is a tunable nudge (currently 0).
- **Actionability is weighted in scoring.** The LLM prompt rewards items that imply a concrete
  decision/response (competitor builds a hospital → growth strategy) and penalizes passive,
  informational items (a routine tropical-weather outlook). Core clause is in `llm_relevance.SYSTEM`;
  the detailed rubric + examples live in `settings.yaml: briefing.relevance_guidance` (tune there,
  no code change), injected into the scoring prompt via `score_batch(..., guidance=...)`.
- **Semantic dedup, not string matching.** The same event from multiple feeds has different
  wording; embeddings catch meaning. String matching was proven to over/under-merge.
- **3-day window by PUBLISH date** so a Monday run covers the weekend and old surfaced
  stories don't sneak in. Undated candidates are **date-enriched** first — we fetch the
  article page and read its publish metadata (`src/ingest/enrich.py`) so the 72h filter
  runs on a true date, not fetch time. Only if enrichment also fails do we fall back to
  fetch time (a rare exception now, not the rule), so nothing provably older than 72h
  gets through while genuinely-recent undated stories are still kept.
- **Per-source cap** stops a high-volume feed (Fierce, Miami Herald) flooding the pool;
  competitors are exempt so their coverage is never trimmed.
- **Scouts run daily (not one-shot).** `stop_after_first_update: false`. Briefing only
  polls (free); scouts scan once/day (the only Yutori charge).
- **Quiet-day email** so silence ≠ broken pipeline.
- **24h re-brief window, not one-shot.** A briefed story stays eligible until
  `rebrief_after_hours` (24h) after it was *first* briefed, then is suppressed so fresh
  news surfaces. `mark_briefed` stamps a full ISO timestamp and only when `briefed_on IS
  NULL` (re-runs don't push the clock forward). `candidates_recent` includes never-briefed
  items plus those briefed within the window. This makes same-day re-runs reproduce the
  same briefing instead of burning a new top-5 every run, while next-day runs roll over.

## Operations

- **Run manually:** `python3 run_briefing.py` (real send) / `--dry-run` (preview to
  `data/briefings/`). On the user's Mac it's `python3` (Python 3.9).
- **Automation:** an external cron service (cron-job.org) triggers the briefing via
  `workflow_dispatch` ~6:07am ET weekdays (GitHub's `schedule:` cron is backup only — it
  drops runs). Secrets (GEMINI_API_KEY, YUTORI_API_KEY, SMTP_HOST/PORT/USER/PASS,
  EMAIL_FROM, ALERT_EMAIL_TO) are set in the repo; the external trigger uses a fine-grained
  PAT with `actions: read/write` stored in cron-job.org. The dedup DB (`data/intel.db`) is
  **committed back to the repo** after each successful run (step "Persist updated database",
  GITHUB_TOKEN with `contents: write`). NOTE: `.gitignore` was excluding `intel.db` until
  2026-06-17 (commit `2e9de61`) — it is now force-added/tracked, so dedup memory is durable
  and shared by local + CI runs. (Recipients/config also come from the repo, so config
  changes require a push to take effect.)
- **Add competitor scouts:** `python3 scripts/setup_scouts.py --force --sources "Name1,Name2"`
  (names from sources.yaml). Each scout ≈ $0.35/scan/day ≈ $10.50/month.
- **Email:** from um.marketintel.bot@gmail.com (Gmail App Password in SMTP_PASS).

## Costs

- Gemini ≈ a few cents/run (scoring + synthesis + embeddings); billing enabled.
- Yutori = **$0.35 per scout-scan**; 2 daily scouts ≈ $21/month. RSS/GNews free.

## Gotchas (learned the hard way)

- **The cloud sandbox has no outbound network** to api.yutori.com, Gemini, or SMTP, and
  **can't install PyPI packages**; SQLite on the mounted folder throws "disk I/O error".
  So scouts/sends/scoring can't be tested from the sandbox — only locally or in CI. To
  inspect the DB in the sandbox, copy it to /tmp first (it's read-only-ish on the mount).
- **Python 3.9 on the user's Mac** — every module needs `from __future__ import annotations`
  for `X | None` hints. CI uses 3.12.
- **GitHub token** is stored in plaintext in the repo's remote URL — recommend rotating.
- Stale `.git/index.lock` can block git; `rm -f .git/index.lock` clears it.
- GitHub Actions auto-pauses a schedule after 60 days of no repo activity. The daily DB
  commit-back counts as activity, so the schedule stays alive on its own.
- **GitHub's `schedule:` cron silently drops/delays runs** — it skipped the daily briefing
  entirely on 2026-06-16 and 06-17 even though the repo was active and the workflow valid.
  Do not rely on it for delivery; the external cron → `workflow_dispatch` trigger is the
  source of truth now (`CLOUD_SCHEDULING.md`). The guard makes the leftover backup cron safe.

## Open / next steps

1. **Finish the reliable-scheduling cutover (`CLOUD_SCHEDULING.md`).** Code is pushed,
   but the external trigger isn't live until you: create the fine-grained PAT, set up the
   two cron-job.org jobs (briefing 6:07am + watchdog 8:12am ET), and confirm the
   `ALERT_EMAIL_TO` repo secret is set to `wef28@miami.edu`. Until then the daily run still
   depends on GitHub's unreliable cron.
2. Scale competitor scouts beyond Baptist/Jackson (uncapped_sources already lists them).
3. Rotate the GitHub access token (still plaintext in the remote URL).
4. Bump the workflow actions off Node 20 (checkout@v4, setup-python@v5, upload-artifact@v4)
   before GitHub removes Node 20 from runners on 2026-09-16.
5. Remaining hygiene: a small test suite for dedup/window/scoring; watch repo size as the
   binary DB accumulates history (squash or BFG-prune if it ever gets large).

## Style

- Python 3.9+ compatible, stdlib + `requirements.txt` deps only; small, mostly-pure modules.
- Pipeline state goes through SQLite. Source failures must never crash the run.
- Never hardcode sources/weights/org details in Python — they live in `config/`.
- Secrets only in `.env` (local) or GitHub Actions secrets. Never commit them.
