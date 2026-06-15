# Healthcare Market Intelligence Platform

AI-driven daily executive briefing covering six intelligence areas: National Healthcare
Policy & Industry, South Florida Competitive Intel, Payer & Insurance, Innovation & AI,
Public Health & Geopolitical Risk, and Reputation & Media Monitoring.

**Pipeline:** RSS + Yutori-scout ingestion → SQLite store → 3-day publish-date filter →
LLM relevance scoring (vs. UHealth) → semantic dedup → LLM synthesis → HTML digest email.

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
   can't sneak in. Undated items fall back to fetch time (so a good story is never dropped just
   for lacking a date).

3. **Relevance scoring (`src/prioritize/`).** Every recent article is scored 0–10 by the LLM
   (Gemini) for relevance to UHealth, judged against its intelligence area's key question.
   We deliberately use **pure LLM relevance** (composite weights in `weights.yaml`): keyword
   rules were removed because they were blunt — they surfaced local fluff and missed strong
   stories with unexpected wording. Scoring runs at **temperature 0** so the same article gets
   the same score every run (consistent rankings). A **per-source cap** (`max_per_source`)
   stops a high-volume feed from flooding the pool; competitor sources are exempt
   (`uncapped_sources`) so their coverage is never trimmed.

4. **Semantic dedup (`scoring.semantic_dedupe`).** The same event often arrives from several
   feeds with different wording. We embed each story (Gemini embeddings) and merge ones whose
   *meaning* is near-identical (cosine ≥ `dedup_cosine_similarity`) — keeping the higher-scored
   copy. This generalizes far better than matching words; a keyword fallback only kicks in if
   the embedding call fails.

5. **Synthesis (`src/output/synthesize.py`).** The top stories go to the LLM, which writes the
   per-story narrative (what happened / why it matters to UHealth / exposure / what to watch).
   It's told to produce one story per item (dedup already happened upstream).

6. **Digest email (`src/output/emailer.py`).** Top N stories rendered as HTML with an
   intelligence-area tag + source up top and a larger headline. Publish dates come from Yutori
   first, else from reading the article page's metadata, else shown as "—". If *nothing* clears
   the threshold, a short **quiet-day note** is sent anyway, so silence never looks like a
   broken pipeline.

7. **Scheduling.** Scouts scan once daily at ~6am ET (set at scout creation). The briefing runs
   ~7am ET weekdays via GitHub Actions (`.github/workflows/daily-briefing.yml`), so the fresh
   scan precedes the email. Secrets live in GitHub (never in the repo); dedup memory persists
   between runs via the Actions cache.

**Cost:** Gemini ≈ a few cents/run (scoring + synthesis + embeddings). Yutori = **$0.35 per
scout-scan**, so each competitor scout ≈ $10.50/month at one daily scan. RSS/Google-News is free.

---

## Work Plan

Status date: **June 12, 2026**. Owners: **W** = William, **F** = Fernando, **C** = Christoph.
Full background in `docs/Implementation_Plan.docx`.

### Phase A — INPUTS (data capture & ingestion) · Jun 12–17

| Status | Task |
|---|---|
| Done | RSS ingestion pipeline (22 sources, all 6 areas) | 
| Done | SQLite store, dedup, source-health logging |
| Done | Google News fallback queries for competitor monitoring |
| Open | Fix remaining quiet feeds (Rock Health, CDC, WHO, SFBJ, FL DOH) — `python scripts/verify_sources.py` |
| Done | Yutori access decision (subscription vs. API) — question to Jake |
| Open | Integrate Yutori Scouting API in `src/ingest/yutori.py` (replaces stub) |

**🚩 GATE G1 — Jun 18 review call:** all six areas ingesting reliably; Yutori access approved or explicitly deferred.

### Phase B — DIGESTION (prioritization & calibration) · Jun 15–24

| Status | Task |
|---|---|
| Open | Composite scoring engine (source × category × LLM relevance, threshold 55) |
| Open | Gemini scoring integration (free tier) + score report tool
| Open | Daily calibration runs: `python run_briefing.py --dry-run --no-yutori` + `python scripts/score_report.py` |
| Open | Tune `config/weights.yaml` from team feedback (source weights, threshold, keywords) |
| Open | Confirm competitor watchlist with leadership | 

**🚩 GATE G2 — Jun 24:** team agrees the top stories are the right stories for 3 consecutive days.

### Phase C — OUTPUT (briefing & delivery) · Jun 22 – Jul 1

| Status | Task |
|---|---|
| Done | Executive summary synthesis + HTML email template |
| Open | Gmail App Password setup; first real send to project team only |
| Open | GitHub Actions secrets + scheduled daily run (workflow already in repo) |
| Open | 5 consecutive automated dry-run deliveries reviewed by team |

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
| `GEMINI_API_KEY` | aistudio.google.com (free tier — default provider) |
| `ANTHROPIC_API_KEY` | console.anthropic.com (only if `llm.provider: anthropic`) |
| `YUTORI_API_KEY` | platform.yutori.com (pending procurement — use `--no-yutori`) |
| `SMTP_USER` / `SMTP_PASS` | Gmail address + App Password (Google Account → Security → 2-Step Verification → App passwords) |
| `EMAIL_TO` | comma-separated recipients |

## Running

```bash
python scripts/verify_sources.py                       # check all RSS feed URLs
python run_briefing.py --dry-run --no-yutori --no-llm  # free ingestion test
python run_briefing.py --dry-run --no-yutori           # full dry run -> data/briefings/
python scripts/score_report.py                         # why each story ranked where it did
python run_briefing.py                                 # real run (sends email)
```

## Scheduling the daily run

**Option A — GitHub Actions (recommended):** `.github/workflows/daily-briefing.yml` runs every
weekday morning in the cloud. Add the `.env` values as repository secrets
(Settings → Secrets and variables → Actions). No computer needs to be on.

**Option B — local cron (macOS/Linux):**
```
0 7 * * 1-5 cd /path/to/marketintel && .venv/bin/python run_briefing.py
```

## Tuning

All scoring behavior lives in `config/weights.yaml` (category/source weights, composite mix,
threshold, keyword boosts) and `config/settings.yaml` (org profile, key questions, LLM
provider/models, lookback window). No code changes needed to retune.

## Working on this repo with Claude Code

`CLAUDE.md` gives Claude Code full project context. Open a terminal in the repo, run
`claude`, and ask for a task, e.g. "implement the Yutori Scouting API in src/ingest/yutori.py".

## Team workflow

`git pull` before you start · commit small, push often · never commit `.env` or API keys ·
weights changes get a one-line rationale in the commit message.
