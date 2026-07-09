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
- **Story reconciliation added (2026-07-09).** The 07-09 briefing shipped a duplicate,
  half-baked story card at the top (raw headline, empty "Why it matters"): the model
  mangled a ~500-char Google-News URL and rewrote the title, so the old url/title
  "safety net" both appended a bare stub for the "missing" article AND failed to attach
  the score/date to the real synthesized story (which then sorted last). Fix: synthesis
  now echoes each item's `[n]` index as `"id"`; `_reconcile_stories()` in
  `run_briefing.py` matches stories back by id (url/title fallback), restores the
  canonical DB url, attaches score/date meta, rejects duplicates and blank-section
  stories, re-synthesizes only the missing items once, and DROPS (never stubs) anything
  still unsalvageable — dropped items stay unbriefed so they retry next run. The
  renderer also skips empty labeled sections. Regression test:
  `python3 scripts/test_reconcile.py` (no network/DB needed).
- **Scheduling is external-cron-driven (changed 2026-06-17).** GitHub's own `schedule:`
  cron proved unreliable — it silently dropped the daily run on 2026-06-16 and 2026-06-17
  (Actions tab showed no run; last was 06-15). The **reliable trigger is now an external
  cron service (cron-job.org) calling the `workflow_dispatch` API** each weekday ~6:07am ET.
  GitHub's `schedule:` cron is kept only as a free backup; a **same-day guard**
  (`scripts/guard_skip_if_ran.py`) + a `concurrency` group ensure the briefing sends at
  most once if both fire. Full setup in `CLOUD_SCHEDULING.md`. Yutori scouts scan at 5am ET.
- **Provider:** Gemini, with **billing enabled** (no longer on the flaky free tier).
- **Scouts:** 2 active competitor scouts (Baptist Health, Jackson Health), scanning daily.
- **Recipients (2026-06-18):** wef28, jakeherman, psharma, cjvonherrath, fxs1141 @miami.edu (5).
- **Prioritization overhauled (2026-06-17/18).** Scoring is now an EXEC-facing
  "impact on UHealth's strategy & long-term direction" rubric (three gates, seven strategic
  vectors, hard exclusions, deterministic floors, example-calibrated). It lives entirely in
  `config/settings.yaml → briefing.relevance_guidance` — that block is the scoring brain.
  Full inventory in `PRIORITIZATION_CHANGELOG.md`. See "Prioritization (current)" below.
- All work committed and pushed to `github.com/fitassepulveda/marketintel` (branch `main`).
  NOTE: the CI bot auto-commits `data/intel.db` on each run, so the remote moves ahead often
  — a manual push usually needs `git checkout -- data/intel.db && git pull --no-rebase` first.

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
        LLM relevance scoring 0-10 (Gemini, temp 0) vs the relevance_guidance rubric
                                  v
        deterministic FORCED-FLOOR rules (e.g. FIU+Baptist same-sentence -> >=9), then
        composite = llm·10 (source/category weights still 0.0 -> 100% LLM relevance)
                                  v
        drop below score_threshold (55) -> sort -> SEMANTIC dedup (embeddings)
                                  v
        SELECT: every story with composite >= 90, min 5, max 12  (was a fixed top-5)
                                  v
        LLM synthesis (Gemini) -> per-story narrative JSON (watch_next now includes a
        model-judged time horizon)
                                  v
        HTML digest email via SMTP — each story shows its LLM relevance badge, plus an
        "Also considered" list of the next 5 runners-up (title+link+score). Files saved
        to data/briefings/. If nothing qualifies: a short "quiet-day" email instead.
```

Entry point: `run_briefing.py`. Flags: `--dry-run` (build + save, don't send),
`--no-yutori`, `--no-llm`.

## Code map

- `run_briefing.py` — orchestrates: `ingest()` → `prioritize()` → synthesis →
  `_reconcile_stories()` (id-based story↔article matching, canonical urls, meta attach,
  dupe/blank rejection, retry-then-drop for missing) → digest/send.
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
  embeddings), `dedupe_by_title()` (keyword fallback), and **`forced_floor()`** — the
  deterministic config-driven score floor (`briefing.forced_floor_rules`; fires when one
  sentence contains a term from every group; never lowers a higher LLM score).
- `src/prioritize/llm_relevance.py` — batched 0-10 scoring (batch_size 15, max_tokens
  4000). The system prompt = base + `briefing.relevance_guidance` (the rubric).
- `src/output/synthesize.py` — briefing JSON; one story per item. `watch_next` asks the
  model for a story-appropriate TIME HORIZON (days … year), not a fixed "1-2 weeks".
- `src/output/emailer.py` — `render_digest` / `render_digest_html` (sent email: area tag,
  source, title + **LLM-relevance badge**, then an **"Also considered" runners-up list**
  via `_runners_html`/`_runner_lines_text`), `render_quiet_html`, `_fmt_score`, `send()`.
- `run_briefing.prioritize()` — applies forced-floor, the >=90/min5/max12 selection, and
  returns `(final, runners)`. Re-brief eligibility is **calendar-day** (see below).
- `scripts/setup_scouts.py` — create/manage scouts: `--list`, `--dry-run`, `--force`
  (archives the old scout first to avoid orphan billing), `--sources`, `--stop`,
  `--restart`. Schedules first run at `scout_scan_hour_local` (5am) and asks for
  `published_date`. **Must run locally** (needs network to api.yutori.com).
- `scripts/verify_sources.py` — checks RSS URLs. `scripts/score_report.py` — debug ranking.
- `scripts/test_synthesis.py` (+ `scripts/test_article.json`) — run a hand-written article
  through the REAL scorer + forced-floor + Gemini synthesis + email render, no DB/send. Prints
  the score (flags a fired floor) and writes a per-article HTML preview. For testing tuning.
- `scripts/purge_stale_undated.py` — one-off cleanup of stale undated rows (used for the old
  native-feed Fierce leftovers). `scripts/diagnose_enrich.py` — probe why date-enrichment fails.
- `PRIORITIZATION_CHANGELOG.md` — the full inventory of every prioritization adjustment/feature.
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
  `org.timezone` America/New_York. `org.description` now carries UHealth's real profile
  (scale, expansion zones, NIH/research, AI, payer, partnerships) — used as the scoring lens.
- `llm.provider: gemini`; scoring & synthesis both `gemini-2.5-flash`.
- `briefing.relevance_guidance` — **the scoring rubric** (gates, vectors, exclusions, worked
  examples). The single biggest tuning surface; no code change needed.
- `briefing.select_threshold: 90`, `min_stories: 5`, `max_stories: 12`, `digest_top_n: 12`
  (selection = every story >=90, min 5 / max 12; replaced the old `max_stories: 5` top-N).
- `briefing.forced_floor_rules` — deterministic floors (FIU+Baptist same-sentence -> 9).
- `briefing.lookback_hours: 72`, `enrich_publish_dates: true`, `enrich_timeout_seconds: 10`.
  `rebrief_after_hours: 24` is **no longer used** — re-brief is now CALENDAR-DAY (below).
- `briefing.digest_recipients`: 5 (wef28, jakeherman, psharma, cjvonherrath, fxs1141 @miami.edu).
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
Becker's) — terms prohibit it; use headline RSS proxies instead. (NOAA NHC weather feed
removed 2026-06-18 — undateable, low-relevance noise.)

## Prioritization (current) — the scoring brain

Everything below is in `config/settings.yaml → briefing.relevance_guidance` (plain-text rubric
injected into the scoring prompt) unless noted. Full inventory: `PRIORITIZATION_CHANGELOG.md`.
Audience = UHealth EXECUTIVES; score = **impact on UHealth's strategy & long-term direction**
(competitive intel fused with operational/financial/strategic impact). See the persisted memory
note "prioritization-audience-and-scoring".

- **Three gates** (applied before placing on the 0-10 scale): (1) direct UHealth relevance —
  not pharma/PBM/insurer; third-party fights 2-3 unless direct impact; (2) actionability —
  general/awareness 3-5, opinion 1; (3) judge against UHealth's EXISTING position — relevance is
  a real gap/threat/opportunity, not a keyword match (e.g. talent pipelines = low, UHealth is
  already strong via the med school).
- **Seven strategic vectors score high (8-10):** growth/competitive footprint (weighted to
  expansion zones), federal/state funding & policy (NIH/Medicare/Medicaid), payer/reimbursement
  leverage, flagship service lines (as a lens for EXTERNAL developments), workforce/talent,
  AI/health-tech (gated), partnerships. Vectors say WHAT can score high; gate 3 decides whether
  it actually moves UHealth's position.
- **Hard exclusions / de-prioritizations:** pharmacy/drug-pricing/PBM is NOT a priority;
  EXCLUDE UHealth's own news (own institutes -> 1-2); AI gate (only if adoptable by a provider
  or a peer system is deploying — biotech/pharma AI & vendors UHealth doesn't use -> 2-4);
  human-interest/survivorship/local public-health warnings -> 1-3; cybersecurity & research are
  slow-day "keep an eye on" awareness (5-6), not weighted vectors.
- **Calibrated by worked examples** drawn from leadership's own review + daily-review notes.
- **Deterministic forced floors** (`forced_floor_rules` + `scoring.forced_floor`): FIU+Baptist
  same-sentence -> >=9 (Baptist moving into academic medicine via FIU's med school). A floor,
  not a cap; config-driven and extensible.
- **Composite & selection:** composite = LLM x 10 (area/source weights off). Select every story
  >=90, **min 5 / max 12** (replaced fixed top-5).
- **Re-brief = CALENDAR DAY** (`run_briefing.prioritize`, org timezone): a briefed story is
  eligible only for the rest of the SAME local day, so same-day re-runs reproduce but a story
  NEVER repeats on a later day. (The old rolling-24h window let late-day stories reappear the
  next morning — fixed 2026-06-18.)

## Design decisions worth remembering

- **Pure LLM relevance, no keywords.** Keyword gating/boosts were removed — they surfaced
  local fluff (e.g. "Miami" matching sports) and missed well-worded stories. The LLM judges
  relevance to UHealth via the `relevance_guidance` rubric; area weight is a tunable nudge (0).
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
- **Re-brief = CALENDAR DAY (changed 2026-06-18, replaced the old rolling-24h window).** A
  briefed story is eligible only for the rest of the SAME local day (org timezone): the cutoff
  passed to `candidates_recent` is local midnight today (in UTC), computed in
  `run_briefing.prioritize`. So same-day re-runs reproduce the briefing, but a story NEVER
  repeats on a later day. `mark_briefed` still stamps a full ISO timestamp only when
  `briefed_on IS NULL`. The old `rebrief_after_hours` (24h) is unused — the rolling window let a
  story briefed late one day reappear the next morning when two runs landed <24h apart.

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
