# Scoring Methodology

How the platform decides which stories reach the executive briefing, **as the
system currently works** (post the 2026-06-17 prioritization rework — see
`PRIORITIZATION_CHANGELOG.md`). Most scoring behavior is tunable in config with no
code change.

> Audience and lens: the briefing is for **UHealth (University of Miami Health
> System)** leadership. Every story is scored by its **impact on UHealth's strategy
> and long-term direction**, not by keyword matching.

---

## Pipeline at a glance

```
RSS feeds + Yutori scouts
   │  exact-URL dedup at ingest → SQLite (data/intel.db)
   ▼
keep only items PUBLISHED in the last lookback window (72h; publish date is
   page-enriched when missing, else fetch-time fallback)
   ▼
per-source cap (max_per_source; competitor feeds exempt) → candidate pool
   ▼
(1) LLM RELEVANCE 0–10  vs the area's key question + the relevance_guidance rubric
   ▼
(2) FORCED FLOORS       deterministic config rules (e.g. FIU+Baptist → 9)
   ▼
(3) COMPOSITE 0–100     currently = 100% LLM relevance (source/category weights off)
   ▼
(4) THRESHOLD           drop below score_threshold (55)
   ▼
(5) SEMANTIC DEDUP      collapse same-event stories by embedding similarity
   ▼
(6) SELECTION           all ≥ select_threshold (90); min 5 / max 12 stories
   ▼
synthesis (LLM) → HTML digest email (score badge per story + "also considered")
```

If nothing clears the threshold, a short **quiet-day note** is sent so silence is
never mistaken for a broken pipeline.

---

## Stage 1 — LLM relevance (0–10)

Each candidate is scored by Gemini against its intelligence area's **key question**
(`config/settings.yaml → key_questions`), with detailed direction injected from
`briefing.relevance_guidance` (tunable with no code change). The rubric is strict
and exec-facing:

- **Gate 1 — direct UHealth relevance.** UHealth is a provider/academic system, not
  a pharma/PBM/insurer; third-party disputes score low unless they hit UHealth.
- **Gate 2 — actionability.** "State of the market" / awareness / routine
  regulatory color caps at 3–5; opinion pieces score 1.
- **Gate 3 — judged against UHealth's existing position.** Relevance means a real
  gap, threat, or opportunity — not a topic match.

Scores high (8–10): competitor capital/capacity/M&A moves in South Florida;
federal/state funding & reimbursement (NIH, Medicare/Medicaid, 340B, GME); payer
leverage affecting UHealth's contracts; flagship-service-line developments;
workforce/talent; adoptable AI. Scored low/excluded: UHealth's own news, pharmacy/
drug-pricing/PBM, human-interest, generic "AI is transforming healthcare."

Output per item: a 0–10 score plus a one-line rationale (both stored; visible via
`scripts/score_report.py`).

---

## Stage 2 — Forced floors (deterministic)

After the LLM, config-driven rules in `settings.yaml → forced_floor_rules` can
*raise* a score (never lower it). The flagship rule: any single sentence naming
**FIU** (or "Florida International University") **and Baptist** is floored at **9**
— Baptist's move into academic medicine via FIU's med school is a direct threat to
UHealth's positioning. Implemented in `scoring.forced_floor`.

---

## Stage 3 — Composite score (0–100)

```
composite = 100 × ( source_weight·source + category_weight·category + llm_weight·relevance )
```

with the mix in `config/weights.yaml → composite`. **Current setting is
`source 0.0 / category 0.0 / llm 1.0` → the composite is 100% LLM relevance.** This
was a deliberate move away from the deck's blend: keyword and area weighting
surfaced local noise and missed well-worded stories, so relevance to UHealth (as
judged by the LLM) now drives ranking on its own. The deck's `category_weights`
(SF Competitive 10 … Reputation 3) and `source_weights` remain in the config and
can be re-activated by changing the mix — they are simply zeroed today.

---

## Stage 4 — Threshold, dedup, selection

- **Threshold:** drop anything below `score_threshold` (**55**).
- **Semantic dedup:** survivors are de-duplicated by **embedding similarity**
  (`dedup_cosine_similarity` 0.85) so the same event across feeds collapses to one
  story; a keyword rule is the fallback if embeddings are unavailable.
- **Selection:** include every story at/above `select_threshold` (**90**), but never
  fewer than `min_stories` (**5**) nor more than `max_stories` (**12**). Strong days
  surface more; quiet days still show a baseline. The next 5 below the cut appear as
  an **"also considered"** list in the email.

A briefed story is suppressed on later days (24h / rest-of-day re-brief window) so
the same item doesn't repeat morning to morning.

---

## Where to tune what

| You want to… | Edit | Code change? |
|---|---|---|
| Change how stories are judged (the rubric, examples, gates) | `settings.yaml → briefing.relevance_guidance` | No |
| Add a deterministic floor rule | `settings.yaml → briefing.forced_floor_rules` | No |
| Re-activate source/area weighting | `weights.yaml → composite` mix | No |
| Change the noise floor / selection bar | `weights.yaml score_threshold`, `settings.yaml select_threshold / min_stories / max_stories` | No |
| Dedup strictness | `weights.yaml dedup_cosine_similarity` | No |
| Per-source flood cap / exemptions | `weights.yaml max_per_source / uncapped_sources` | No |
| Add/adjust sources | `sources.yaml` | No |

Inspect results with `python scripts/score_report.py` (per-story breakdown) and
`python run_briefing.py --dry-run` (build without sending).

---

## Analysis layer (on top of scoring)

Two additive tools help *understand and calibrate* the scoring without changing it:

- **Structured sub-scores** (`src/prioritize/subscores.py`): rate each article on
  six universal dimensions (financial / strategic / competitive / operational /
  time-sensitivity / proximity), stored in SQL.
- **AHP + eigen-analysis** (`scripts/ahp.py`, `scripts/ahp_to_excel.py`) and
  **per-executive personalization** (`config/profiles.yaml`, `scripts/personalize.py`).

See `docs/AHP_ANALYSIS.md` for the full write-up.
