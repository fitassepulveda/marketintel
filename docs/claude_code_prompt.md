# Claude Code onboarding prompt

Paste the block below at the start of a Claude Code session in this repo.

---

You are helping me work on a healthcare market-intelligence platform. Before doing
anything, read these files to load full context: `CLAUDE.md` (architecture + gotchas),
`README.md` (the dated work plan), `PRIORITIZATION_CHANGELOG.md`, and everything in
`docs/` (especially `SCORING.md`, `AHP_ANALYSIS.md`, and `yutori_vs_rss.md`).

What the project does: a daily AI briefing for executives of UHealth (University of
Miami Health System). It ingests news across six intelligence areas (national policy,
South Florida competitive, payer, innovation/AI, public health, reputation), scores
each story 0–10 for strategic relevance with an LLM, deduplicates, synthesizes an
executive summary, and emails it each weekday morning.

Stack and key facts:
- Python. Storage is SQLite in `data/intel.db` (committed to the repo — NOT MySQL,
  no server). LLM is Gemini by default (free tier; provider is switchable in
  `config/settings.yaml`). External monitoring is Yutori (Scouting API for competitors,
  Research API for per-story deep-dives). Email via SMTP. Scheduling via GitHub Actions.
- Entry point: `run_briefing.py` with flags `--dry-run`, `--no-yutori`, `--no-llm`.
- Code map: `src/ingest/` (rss, yutori, enrich, deep_dive), `src/prioritize/`
  (scoring, llm_relevance, subscores), `src/output/` (synthesize, emailer),
  `src/config.py`, `src/store.py`, `src/llm_client.py`. Scripts in `scripts/`.
- All tunable behavior lives in `config/*.yaml` — never hardcode sources, weights,
  org details, or the scoring rubric. The "scoring brain" is
  `config/settings.yaml → briefing.relevance_guidance`.

How to run and test (do this, don't guess):
- Always `source .venv/bin/activate` first — a fresh terminal opens in `(base)` without
  the dependencies.
- Free, no-API test of the whole pipeline: `python run_briefing.py --dry-run --no-yutori --no-llm`.
- Full dry run (uses Gemini, no email): `python run_briefing.py --dry-run`.
- Inspect ranking: `python scripts/score_report.py`.

Rules and gotchas to respect:
- Secrets (API keys, SMTP password) live only in `.env` (local) or GitHub Actions
  secrets — never commit them, never print them. Strip whitespace when reading keys.
- I work with a teammate who pushes often: ALWAYS `git pull --no-rebase origin main`
  before committing, and prefer additive changes (new files) over editing shared files
  when reasonable, to avoid merge conflicts.
- Source/network failures must never crash the run; keep changes fail-safe.
- Local Python may be 3.9 (use `from __future__ import annotations` for `X | None`
  hints); CI uses 3.12.
- The SQLite DB is committed and auto-updated by CI — don't hand-edit rows; copy the
  file first if you want to experiment.

How to work with me: I am a product lead, not a deep coder. Explain what each change
does and why in plain language, show me the diff, and when you debug, read the relevant
module AND its config before proposing a fix. When changing scoring behavior, prefer a
config edit over code. Keep me informed of cost (Gemini calls, Yutori $0.35/task) and
anything that affects the morning send.

My current task is: [DESCRIBE WHAT YOU WANT TO DO].

---
