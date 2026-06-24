# RSS Feed vs. Yutori Pull — what the data actually looks like

The pipeline ingests from two very different kinds of source. This explains the
difference, with real example payloads, so it's clear why we use each one and what
each contributes.

---

## The one-sentence version

**RSS gives you a headline and a blurb that a website chose to publish. A Yutori
pull gives you a researched, structured answer — the model reads multiple pages and
returns clean fields (summary, source URL, date, citations) you can act on.**

RSS is free, instant, and broad but shallow. Yutori is paid, slower, and narrow but
deep. We use RSS for volume coverage and Yutori for the sources RSS can't reach
(competitor newsrooms, business journals) and for deep-dives on the day's top stories.

---

## What an RSS feed item looks like

An RSS feed is an XML document a website publishes. Each `<item>` is one story, and
it contains only what the publisher put there — usually a title, a link, a short
summary, and a date. There is no analysis and no full text.

Raw XML (trimmed):

```xml
<item>
  <title>CMS proposes 2.4% Medicare payment update for hospitals</title>
  <link>https://www.example.gov/newsroom/cms-2027-ipps-proposed-rule</link>
  <description>The proposed rule would raise inpatient rates and revise
    quality-reporting requirements for fiscal year 2027.</description>
  <pubDate>Tue, 24 Jun 2026 13:00:00 GMT</pubDate>
</item>
```

After our RSS reader parses it, it becomes this article record:

```json
{
  "title": "CMS proposes 2.4% Medicare payment update for hospitals",
  "url": "https://www.example.gov/newsroom/cms-2027-ipps-proposed-rule",
  "summary": "The proposed rule would raise inpatient rates and revise quality-reporting requirements for fiscal year 2027.",
  "published": "2026-06-24T13:00:00+00:00",
  "source": "CMS Newsroom",
  "area": "national_policy"
}
```

What you get: the headline, a one- or two-sentence blurb, a link, a date. What you
don't get: the full article, any interpretation, any related context, or anything
from a site that doesn't publish a feed (most competitors and business journals).

---

## What a Yutori pull looks like

Yutori runs a web agent that actually reads pages and returns **structured** data in
a schema we request. We use it two ways.

### (a) Scouting pull — continuous competitor monitoring

A Scout watches a competitor and, when something material happens, returns an update
whose `structured_result` is an array of clean records, plus the source citations:

```json
{
  "id": "upd_8f12...",
  "timestamp": 1782312000,
  "content": "Baptist Health announced a $250M expansion of its Miami Cancer Institute...",
  "structured_result": [
    {
      "headline": "Baptist Health to invest $250M expanding Miami Cancer Institute",
      "summary": "Baptist Health will add a proton-therapy center and 80 beds at its Kendall campus, opening 2028, deepening its oncology footprint in west Miami-Dade.",
      "source_url": "https://baptisthealth.net/news/miami-cancer-institute-expansion",
      "published_date": "2026-06-24"
    }
  ],
  "citations": [
    { "url": "https://baptisthealth.net/news/miami-cancer-institute-expansion" },
    { "url": "https://www.bizjournals.com/southflorida/news/2026/06/24/baptist-cancer.html" }
  ]
}
```

Normalized into the same article shape the pipeline uses everywhere:

```json
{
  "title": "Baptist Health to invest $250M expanding Miami Cancer Institute",
  "url": "https://baptisthealth.net/news/miami-cancer-institute-expansion",
  "summary": "Baptist Health will add a proton-therapy center and 80 beds at its Kendall campus, opening 2028, deepening its oncology footprint in west Miami-Dade.",
  "published": "2026-06-24",
  "source": "Baptist Health South Florida",
  "area": "south_florida_competitive",
  "enrichment": { "citations": ["https://baptisthealth.net/...", "https://www.bizjournals.com/..."] }
}
```

### (b) Research pull — a one-time deep-dive on a single story

This is the new per-story enrichment. For a story already on the brief, we ask Yutori
to go gather additional context. We send a query + the output schema we want, and after
the task finishes we poll for the structured answer:

```json
{
  "status": "succeeded",
  "structured_result": {
    "additional_context": "The expansion follows Baptist's 2025 partnership with MD Anderson and a regional shortage of proton-therapy capacity; it directly overlaps UHealth Sylvester's oncology catchment in west Miami-Dade.",
    "key_facts": [
      "$250M capital commitment; 80 new beds",
      "Proton-therapy center — only the second in South Florida",
      "Targeted opening 2028; Kendall campus (≈6 miles from UHealth Kendall)"
    ],
    "implication": "Direct competitive pressure on Sylvester's oncology growth in a key expansion zone; watch for referral-pattern and recruiting effects.",
    "sources": [
      "https://baptisthealth.net/news/miami-cancer-institute-expansion",
      "https://www.bizjournals.com/southflorida/news/2026/06/24/baptist-cancer.html",
      "https://www.miamiherald.com/news/health-care/article-...html"
    ]
  }
}
```

That depth — synthesized context, discrete facts, a stated implication, and multiple
corroborating sources — is impossible from RSS alone.

---

## Side-by-side

| | RSS feed item | Yutori pull |
|---|---|---|
| Source | Whatever the site publishes in its feed | Any public web page, read by an agent |
| Content | Title + short blurb + link | Full structured answer: summary, facts, citations, dates |
| Interpretation | None | Synthesized context and implications |
| Coverage | Only sites that offer a feed | Competitor newsrooms, business journals, anything online |
| Freshness | Instant on publish | Minutes (agent has to read pages) |
| Cost | Free | ~$0.35 per scout-scan / per research task |
| Reliability | Very high (just reading a file) | High, but can be blocked or rate-limited |
| Best for | High-volume baseline coverage | Non-RSS sources + deep-dives on top stories |

---

## How they fit together in our pipeline

1. **RSS** does the broad daily sweep across policy, payer, innovation, and public-health
   feeds — free and complete.
2. **Yutori Scouts** cover the competitors and journals that have no usable feed,
   returning structured, citation-backed findings.
3. **Yutori Research** (the new step) runs a deep-dive on each story selected for the
   briefing, adding context, key facts, an implication for UHealth, and extra sources.

Every one of these is normalized into the same article record, so the scoring,
deduplication, and synthesis stages treat them identically — the only difference is how
much depth each carries.
