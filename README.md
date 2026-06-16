# Healthcare Market Intelligence Platform

AI-driven daily executive briefing covering six intelligence areas: National Healthcare
Policy & Industry, South Florida Competitive Intel, Payer & Insurance, Innovation & AI,
Public Health & Geopolitical Risk, and Reputation & Media Monitoring.

**Pipeline:** RSS + Yutori-scout ingestion → SQLite store → 3-day publish-date filter →
LLM relevance scoring (vs. UHealth) → semantic dedup → 24h re-brief suppression →
LLM synthesis → HTML digest email. A separate watchdog alerts if a day's run is missed.

---

## How it works & why (build notes)

Step-by-step of what the pipeline does each run, and the reasoning behind each choice.

1. **Ingest (`src/ingest/`).** Free RSS/Google-News feeds cover most sources. Competitor
   health systems (Baptist, Jackson, …) have no usable RSS, so they're monitored by
   **Yutori "scouts"** — agents that read the newsroom pages and return structured findings.
   Everything lands in one SQLite file (`data/intel.db`), deduped on exact URL at write time.

2. **3-day window by *publish* date (`run_briefing._is_recent`).** We keep only stories
   published in the last 72h — so a Monday run covers the weekend back to Friday. We filter on
   the article's *publish* date, not when we fetched it, so an old story a scout surfaces today
   can't sneak in. Items that arrive **undated** are first date-enriched (`src/ingest/enrich.py`):
   we fetch the article page and read the publish date the site records, so the window filters
   on a true date. Only if that also fails do we fall back to fetch time — a rare exception, so
   nothing provably older than 72h gets through, while genuinely-recent undated stories are kept.

3. **Relevance scoring (`src/prioritize/`).** Every recent article is scored 0–10 by the LLM
   (Gemini) for relevance to UHealth, judged against its intelligence area's key question.
   We deliberately use **pure LLM relevance** (composite weights in `weights.yaml`): keyword
   rules were removed because they were blunt — they surfaced local fluff and missed strong
   stories with unexpected wording. Scoring runs at **temperature 0** so the same article gets
   the same score every run (consistent rankings). The prompt also weights **actionability** —
   items implying a concrete decision/response (e.g. a competitor building a hospital) score
   above passive, informational ones (e.g. a routine weather outlook); rubric is tunable in
   `settings.yaml: briefing.relevance_guidance`. A **per-source cap** (`max_per_source`)
   stops a high-volume feed from flooding the pool; competitor sources are exempt
   (`uncapped_sources`) so their coverage is never trimmed.

4. **Semantic dedup (`scoring.semantic_dedupe`).** The same event often arrives from several
   feeds with different wording. We embed each story (Gemini embeddings) and merge ones whose
   *meaning* is near-identical (cosine ≥ `dedup_cosine_similarity`) — keeping the higher-scored
   copy. This generalizes far better than matching words; a keyword fallback only kicks in if
   the embedding call fails.

   *Across days (`store.candidates_recent` / `mark_briefed`):* once a story is briefed it's
   stamped with the time it *first* went out and stays eligible for `rebrief_after_hours`
   (default 24h), then is suppressed. So re-running the same day reproduces the same briefing
   instead of burning a fresh top-5 each run, while the next day's run rolls over to new
   stories. This depends on the SQLite dedup memory persisting between runs (see Scheduling).

5. **Synthesis (`src/output/synthesize.py`).** The top stories go to the LLM, which writes the
   per-story narrative (what happened / why it matters to UHealth / exposure / what to watch).
   It's told to produce one story per item (dedup already happened upstream).

6. **Digest email (`src/output/emailer.py`).** Top N stories rendered as HTML with an
   intelligence-area tag + source up top and a larger headline. Publish dates come from Yutori
   first, else from reading the article page's metadata, else shown as "—". If *nothing* clears
   the threshold, a short **quiet-day note** is sent anyway, so silence never looks like a
   broken pipeline.

7. **Scheduling.** Scouts scan once daily at 5am ET (set at scout creation). The briefing runs
   ~6:07am ET weekdays via GitHub Actions (`.github/workflows/daily-briefing.yml`; cron `7 10`,
   deliberately off the top of the hour, which GitHub's scheduler delays/drops most often), so
   the fresh scan precedes the email. A **watchdog** (`.github/workflows/briefing-watchdog.yml`,
   ~8:12am ET) checks the GitHub API for a successful briefing run that day and emails an
   `[ALERT]` if none is found — so a silently dropped schedule never goes unnoticed. Secrets
   live in GitHub (never in the repo); dedup memory is durable — `data/intel.db` is committed
   back to the repo after each successful run (shared by local + CI runs, and the daily commit
   also keeps the schedule from auto-pausing).

**Cost:** Gemini ≈ a few cents/run (scoring + synthesis + embeddings). Yutori = **$0.35 per
scout-scan**, so each competitor scout ≈ $10.50/month at one daily scan. RSS/Google-News is free.

---

## Work Plan

Status date: **June 16, 2026**. Owners: **W** = William, **F** = Fernando, **C** = Christoph.
Full background in `docs/Implementation_Plan.docx`.

**Current status:** live and automated end-to-end — Gemini scoring/synthesis (billing
enabled), Yutori scouts running daily (Baptist, Jackson), real sends validated, scheduled
6:07am ET weekday run (scouts scan 5am) + missed-run watchdog in place. Remaining work is calibration, scaling
competitor scouts, and leadership go-live sign-off.

### Phase A — INPUTS (data capture & ingestion) · Jun 12–17

| Status | Task |
|---|---|
| Done | RSS ingestion pipeline (22 sources, all 6 areas) | 
| Done | SQLite store, dedup, source-health logging |
| Done | Google News fallback queries for competitor monitoring |
| Open | Fix remaining quiet feeds (Rock Health, CDC, WHO, SFBJ, FL DOH) — `python scripts/verify_sources.py` |
| Done | Yutori access decision (subscription vs. API) — question to Jake |
| Done | Integrate Yutori Scouting API in `src/ingest/yutori.py` (scouts live: Baptist, Jackson) |

**🚩 GATE G1 — Jun 18 review call:** all six areas ingesting reliably; Yutori access approved or explicitly deferred. *(Yutori live; quiet-feed cleanup ongoing.)*

### Phase B — DIGESTION (prioritization & calibration) · Jun 15–24

| Status | Task |
|---|---|
| Done | Composite scoring engine — tuned to **pure LLM relevance** (threshold 55) |
| Done | Gemini scoring integration (billing enabled) + score report tool |
| Done | 24h re-brief window so daily runs serve fresh stories (`rebrief_after_hours`) |
| Open | Daily calibration runs: `python run_briefing.py --dry-run --no-yutori` + `python scripts/score_report.py` |
| Open | Tune `config/weights.yaml` from team feedback (area weights, threshold) |
| Open | Confirm competitor watchlist with leadership |

**🚩 GATE G2 — Jun 24:** team agrees the top stories are the right stories for 3 consecutive days.

### Phase C — OUTPUT (briefing & delivery) · Jun 22 – Jul 1

| Status | Task |
|---|---|
| Done | Executive summary synthesis + HTML email template |
| Done | Gmail App Password setup; first real send validated |
| Done | GitHub Actions secrets + scheduled daily run (6:07am ET) + missed-run watchdog |
| Open | 5 consecutive automated deliveries reviewed by team |

**🚩 GATE G3 — Jun 30:** five clean automated runs; format approved by S&T leadership.

### Go-Live · week of Jul 6

| Status | Task |
|---|---|---|
| Open | Switch delivery to executive distribution list |
| Open | Leadership sign-off (🚩 GATE G4) |

---

## Setup (5 minutes)

```bash
git clone https://github.com/fitassepulveda/marketintel.git && cd marketintel
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # then fill in your keys (see below)
```

Required keys in `.env`:

| Variable | Where to get it |
|---|---|
| `GEMINI_API_KEY` | aistudio.google.com (billing enabled — default provider) |
| `ANTHROPIC_API_KEY` | console.anthropic.com (only if `llm.provider: anthropic`) |
| `YUTORI_API_KEY` | platform.yutori.com (scouts live; `--no-yutori` to skip) |
| `SMTP_USER` / `SMTP_PASS` | Gmail address + App Password (Google Account → Security → 2-Step Verification → App passwords) |
| `EMAIL_FROM` | sender address shown on the digest |
| `ALERT_EMAIL_TO` | watchdog alert recipient (falls back to `SMTP_USER`) |

Digest **recipients** are set in `config/settings.yaml` (`briefing.digest_recipients`), not
in `.env` — currently `wef28@miami.edu`.

## Running

```bash
python scripts/verify_sources.py                       # check all RSS feed URLs
python run_briefing.py --dry-run --no-yutori --no-llm  # free ingestion test
python run_briefing.py --dry-run --no-yutori           # full dry run -> data/briefings/
python scripts/score_report.py                         # why each story ranked where it did
python run_briefing.py                                 # real run (sends email)
```

## Scheduling the daily run

**Option A — GitHub Actions (recommended, in use):** `.github/workflows/daily-briefing.yml`
runs ~6:07am ET every weekday in the cloud (cron `7 10`). Add the `.env` values as repository
secrets (Settings → Secrets and variables → Actions), plus `ALERT_EMAIL_TO` for the watchdog.
No computer needs to be on. The `briefing-watchdog.yml` workflow emails an alert if a day's run
is missed.

**Option B — local cron (macOS/Linux):**
```
7 10 * * 1-5 cd /path/to/marketintel && .venv/bin/python run_briefing.py
```

## Tuning

All scoring behavior lives in `config/weights.yaml` (category/source weights, composite mix,
threshold) and `config/settings.yaml` (org profile, key questions, LLM provider/models,
lookback window, `rebrief_after_hours`, digest recipients). No code changes needed to retune.

## Working on this repo with Claude Code

`CLAUDE.md` gives Claude Code full project context. Open a terminal in the repo, run
`claude`, and ask for a task, e.g. "implement the Yutori Scouting API in src/ingest/yutori.py".

## Team workflow

`git pull` before you start · commit small, push often · never commit `.env` or API keys ·
weights changes get a one-line rationale in the commit message.
