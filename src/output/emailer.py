"""HTML email rendering + SMTP delivery."""
from __future__ import annotations
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from html import escape

AREA_LABELS = {
    "national_policy": "National Healthcare Policy & Industry",
    "south_florida_competitive": "South Florida Competitive Intel",
    "payer_insurance": "Payer & Insurance Intel",
    "innovation_ai": "Innovation & AI",
    "public_health_risk": "Public Health & Geopolitical Risk",
    "reputation_media": "Reputation & Media Monitoring",
}


def render_html(briefing: dict, date_str: str, org_name: str, failing: list[str]) -> str:
    def sec(title):
        return f'<h2 style="color:#1F3864;font-size:16px;margin:24px 0 8px">{escape(title)}</h2>'

    parts = [
        '<div style="font-family:Arial,sans-serif;max-width:680px;margin:auto;color:#222">',
        f'<h1 style="color:#1F3864;font-size:20px">Market Intelligence Briefing — {escape(date_str)}</h1>',
        f'<p style="color:#666;font-size:12px;margin-top:-8px">{escape(org_name)} · Highly Confidential</p>',
        sec("Top-Line Takeaways"), "<ol>",
        *[f'<li style="margin-bottom:6px">{escape(t)}</li>' for t in briefing.get("takeaways", [])],
        "</ol>",
        sec("Key Question Answers"),
    ]
    for area, answer in briefing.get("key_question_answers", {}).items():
        label = AREA_LABELS.get(area, area)
        parts.append(f'<p style="margin:4px 0"><b>{escape(label)}:</b> {escape(answer)}</p>')

    parts.append(sec("Today's Top Stories"))
    for s in briefing.get("stories", []):
        label = AREA_LABELS.get(s.get("area", ""), s.get("area", ""))
        parts.append(
            '<div style="border-left:3px solid #1F3864;padding:6px 12px;margin:10px 0;background:#F7F9FC">'
            f'<p style="margin:0"><b><a href="{escape(s.get("url", "#"))}" style="color:#1F3864">'
            f'{escape(s.get("title", ""))}</a></b><br>'
            f'<span style="color:#888;font-size:12px">{escape(label)} · {escape(s.get("source", ""))}</span></p>'
            f'<p style="margin:6px 0 0"><b>What happened:</b> {escape(s.get("what_happened", ""))}</p>'
            f'<p style="margin:4px 0 0"><b>Why it matters:</b> {escape(s.get("why_it_matters", ""))}</p>'
            f'<p style="margin:4px 0 0"><b>Exposure:</b> {escape(s.get("exposure", ""))}</p></div>'
        )

    if briefing.get("watch"):
        parts += [sec("Developments to Watch"), "<ul>",
                  *[f"<li>{escape(w)}</li>" for w in briefing["watch"]], "</ul>"]
    if briefing.get("actions"):
        parts += [sec("Recommended Actions & Considerations"), "<ul>",
                  *[f"<li>{escape(a)}</li>" for a in briefing["actions"]], "</ul>"]
    if failing:
        parts.append(
            f'<p style="color:#A33;font-size:11px">Source health alert — no items for 2+ days: '
            f'{escape(", ".join(failing))}</p>'
        )
    parts.append('<p style="color:#999;font-size:11px">Generated automatically by the Market Intelligence Platform.</p></div>')
    return "".join(parts)


def _fmt_date(value: str | None) -> str:
    """Best-effort YYYY-MM-DD from an ISO or RFC-822 date string."""
    if not value:
        return "—"
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return s[:10] if len(s) >= 10 else s


def _fmt_score(value) -> str:
    """Format an LLM relevance score (0-10) for display, or '' if missing."""
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return ""


def _norm_url(u: str | None) -> str:
    """Normalize a URL for matching (drop scheme, www, query, fragment, trailing /)."""
    if not u:
        return ""
    u = str(u).strip().lower().split("?")[0].split("#")[0].rstrip("/")
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if u.startswith("www."):
        u = u[4:]
    return u


def _runner_lines_text(runners: list[dict] | None) -> list[str]:
    """Plain-text 'just missed the cut' comparison list — title, score, link only."""
    if not runners:
        return []
    out = ["", "—" * 30, "ALSO CONSIDERED — closest stories that did not make the cut (for comparison):", ""]
    for a in runners:
        sc = _fmt_score(a.get("llm_score"))
        tag = f" (LLM relevance {sc}/10)" if sc else ""
        out.append(f'- {a.get("title", "")}{tag}\n  {a.get("url", "")}')
    return out


def _runners_html(runners: list[dict] | None) -> str:
    """HTML 'just missed the cut' comparison list — title (linked), score, source only."""
    if not runners:
        return ""
    rows = [
        '<div style="margin-top:30px;border-top:1px solid #ddd;padding-top:14px">'
        '<p style="color:#1F3864;font-size:13px;font-weight:bold;margin:0 0 8px">'
        'Also considered — closest stories that didn\'t make the cut '
        '<span style="color:#888;font-weight:normal">(for comparison)</span></p>'
    ]
    for a in runners:
        sc = _fmt_score(a.get("llm_score"))
        tag = (f'<span style="color:#888">&nbsp;·&nbsp;LLM relevance {sc}/10</span>') if sc else ""
        src = escape(str(a.get("source", "") or ""))
        src_txt = f'<span style="color:#aaa">&nbsp;·&nbsp;{src}</span>' if src else ""
        url = escape(str(a.get("url", "") or "#"))
        title = escape(str(a.get("title", "")))
        rows.append(
            f'<p style="margin:5px 0;font-size:13px">'
            f'<a href="{url}" style="color:#1F3864">{title}</a>{tag}{src_txt}</p>'
        )
    rows.append("</div>")
    return "".join(rows)


def render_digest(stories: list[dict], date_str: str, org_short: str,
                  articles: list[dict] | None = None, top_n: int = 5,
                  runners: list[dict] | None = None) -> str:
    """Plain-text digest of the top N stories in the per-story bullet format.

    `articles` are the source DB rows; we match each story to one (by URL, then by
    title) to fill Captured/Published dates that the LLM doesn't produce.
    """
    articles = articles or []
    by_url, by_title = {}, {}
    for a in articles:
        meta = {"fetched": a.get("fetched"), "published": a.get("published"),
                "source": a.get("source"), "area": a.get("area"),
                "llm_score": a.get("llm_score")}
        if a.get("url"):
            by_url[_norm_url(a["url"])] = meta
        if a.get("title"):
            by_title[str(a["title"]).strip().lower()] = meta

    out = [f"Market Intelligence Briefing — {date_str}", ""]
    for s in stories[:top_n]:
        meta = (by_url.get(_norm_url(s.get("url", "")))
                or by_title.get(str(s.get("title", "")).strip().lower())
                or {})
        captured = _fmt_date(meta.get("fetched"))
        published = _fmt_date(s.get("published") or meta.get("published"))
        src = meta.get("source") or s.get("source", "")
        area = meta.get("area") or s.get("area", "")
        area_label = AREA_LABELS.get(area, area)
        label = s.get("coverage_label") or f'{src or "source"} coverage'
        url = s.get("url", "")
        score = _fmt_score(meta.get("llm_score") if meta.get("llm_score") is not None
                           else s.get("llm_score"))
        title_line = s.get("title", "")
        if score:
            title_line = f'{title_line}  (LLM relevance {score}/10)'
        out += [
            f'[{area_label}]  ·  {src}',
            title_line,
            "",
            f'* What happened: {s.get("what_happened", "")}',
            f'* Why it matters to {org_short}: {s.get("why_it_matters", "")}',
            f'* Institutional exposure: {s.get("exposure", "")}',
            f'* What to watch next: {s.get("watch_next", "")}',
            f'* Supporting coverage: Read more through [{label}]({url})',
            f'* Captured Date: {captured}',
            f'* Published Date: {published}',
            "",
        ]
    for line in _runner_lines_text(runners):
        out.append(line)
    return "\n".join(out).rstrip() + "\n"


def render_digest_html(stories: list[dict], date_str: str, org_short: str,
                       articles: list[dict] | None = None, top_n: int = 5,
                       runners: list[dict] | None = None) -> str:
    """HTML version of the digest — same content, with larger article titles."""
    articles = articles or []
    by_url, by_title = {}, {}
    for a in articles:
        meta = {"fetched": a.get("fetched"), "published": a.get("published"),
                "source": a.get("source"), "area": a.get("area"),
                "llm_score": a.get("llm_score")}
        if a.get("url"):
            by_url[_norm_url(a["url"])] = meta
        if a.get("title"):
            by_title[str(a["title"]).strip().lower()] = meta

    parts = [
        '<div style="font-family:Arial,sans-serif;max-width:680px;margin:auto;'
        'color:#222;font-size:14px;line-height:1.5">',
        f'<p style="color:#1F3864;font-size:15px;font-weight:bold;margin:0 0 4px">'
        f'Market Intelligence Briefing — {escape(date_str)}</p>',
    ]
    for s in stories[:top_n]:
        meta = (by_url.get(_norm_url(s.get("url", "")))
                or by_title.get(str(s.get("title", "")).strip().lower())
                or {})
        captured = _fmt_date(meta.get("fetched"))
        published = _fmt_date(s.get("published") or meta.get("published"))
        src = meta.get("source") or s.get("source", "")
        area = meta.get("area") or s.get("area", "")
        area_label = AREA_LABELS.get(area, area)
        label = s.get("coverage_label") or f'{src or "source"} coverage'
        url = escape(s.get("url", ""))
        score = _fmt_score(meta.get("llm_score") if meta.get("llm_score") is not None
                           else s.get("llm_score"))
        score_html = (
            f'<span style="font-size:13px;color:#6b7a90;font-weight:normal;white-space:nowrap">'
            f'&nbsp;&nbsp;<span style="background:#EAF0F8;color:#1F3864;padding:1px 7px;'
            f'border-radius:10px">LLM relevance {score}/10</span></span>'
        ) if score else ""
        # Additional context (prior coverage) — rendered ONLY when populated.
        ac = s.get("additional_context") or {}
        ac_html = ""
        if ac.get("summary") or ac.get("related") or ac.get("web"):
            def _links(items):
                return " &nbsp;·&nbsp; ".join(
                    f'<a href="{escape(r.get("url",""))}" style="color:#1F3864">'
                    f'{escape((r.get("title") or "source")[:80])}</a>'
                    f'{(" (" + escape(r["date"]) + ")") if r.get("date") else ""}'
                    for r in (items or []) if r.get("url"))
            prior_links = _links(ac.get("related"))
            web_links = _links(ac.get("web"))
            ac_html = (
                f'<p style="margin:5px 0;background:#F7F9FC;border-left:3px solid #6b7a90;'
                f'padding:6px 10px"><b>Additional context:</b> {escape(ac.get("summary",""))}'
                + (f'<br><span style="font-size:12px;color:#666">From the web: {web_links}</span>' if web_links else "")
                + (f'<br><span style="font-size:12px;color:#666">Prior coverage: {prior_links}</span>' if prior_links else "")
                + '</p>')
        parts.append(
            f'<p style="margin:26px 0 2px">'
            f'<span style="background:#1F3864;color:#fff;font-size:11px;font-weight:bold;'
            f'padding:2px 8px;border-radius:3px;letter-spacing:.03em">{escape(area_label)}</span>'
            f'<span style="color:#888;font-size:12px">&nbsp;&nbsp;{escape(src)}</span></p>'
            f'<h2 style="font-size:21px;color:#1F3864;margin:2px 0 8px">'
            f'{escape(s.get("title", ""))}{score_html}</h2>'
            f'<p style="margin:5px 0"><b>What happened:</b> {escape(s.get("what_happened", ""))}</p>'
            f'<p style="margin:5px 0"><b>Why it matters to {escape(org_short)}:</b> '
            f'{escape(s.get("why_it_matters", ""))}</p>'
            f'<p style="margin:5px 0"><b>Institutional exposure:</b> {escape(s.get("exposure", ""))}</p>'
            f'<p style="margin:5px 0"><b>What to watch next:</b> '
            f'{escape(s.get("watch_next", ""))}</p>'
            f'{ac_html}'
            f'<p style="margin:5px 0"><b>Supporting coverage:</b> '
            f'<a href="{url}" style="color:#1F3864">{escape(label)}</a></p>'
            f'<p style="margin:5px 0;color:#666;font-size:12px">'
            f'Captured: {captured} &nbsp;·&nbsp; Published: {published}</p>'
        )
    parts.append(_runners_html(runners))
    parts.append("</div>")
    return "".join(parts)


def render_quiet_html(date_str: str, org_name: str, lookback_hours: int,
                      failing: list[str] | None = None) -> str:
    """Short 'nothing material today' note — sent so a quiet day isn't silent."""
    days = max(1, round(lookback_hours / 24))
    parts = [
        '<div style="font-family:Arial,sans-serif;max-width:680px;margin:auto;'
        'color:#222;font-size:14px;line-height:1.5">',
        f'<p style="color:#1F3864;font-size:15px;font-weight:bold;margin:0 0 8px">'
        f'Market Intelligence Briefing — {escape(date_str)}</p>',
        f'<p>No developments cleared the relevance threshold over the past {days} days — '
        f'nothing material to report this morning for {escape(org_name)}.</p>',
        '<p style="color:#666;font-size:12px">This is an automated note confirming the '
        'briefing ran; it is not a delivery error.</p>',
    ]
    if failing:
        parts.append(
            '<p style="color:#A33;font-size:12px">Note: these sources have returned nothing '
            f'for 2+ days, so coverage may be incomplete — {escape(", ".join(failing))}.</p>'
        )
    parts.append("</div>")
    return "".join(parts)


def send(body: str, subject: str, smtp_cfg: dict, subtype: str = "html"):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["from"]
    msg["To"] = ", ".join(smtp_cfg["to"])
    msg.attach(MIMEText(body, subtype))
    with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg["port"])) as server:
        server.starttls()
        server.login(smtp_cfg["user"], smtp_cfg["password"])
        server.sendmail(smtp_cfg["from"], smtp_cfg["to"], msg.as_string())
