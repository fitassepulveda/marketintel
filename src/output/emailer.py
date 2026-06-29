"""HTML email rendering + SMTP delivery."""
from __future__ import annotations
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from html import escape

import re

AREA_LABELS = {
    "national_policy": "National Healthcare Policy & Industry",
    "south_florida_competitive": "South Florida Competitive Intel",
    "payer_insurance": "Payer & Insurance Intel",
    "innovation_ai": "Innovation & AI",
    "public_health_risk": "Public Health & Geopolitical Risk",
    "reputation_media": "Reputation & Media Monitoring",
}

# Per-area color coding: (accent — used for the left bar + chip text, chip background tint).
AREA_COLORS = {
    "national_policy":           ("#2D9CDB", "#E3F2FB"),
    "south_florida_competitive": ("#C0392B", "#FDECEA"),
    "payer_insurance":           ("#6B3FA0", "#F0EAFB"),
    "innovation_ai":             ("#0B7C77", "#E4F7F5"),
    "public_health_risk":        ("#9A6700", "#FBF1E0"),
    "reputation_media":          ("#475063", "#EEF1F5"),
}
DEFAULT_AREA_COLOR = ("#1F3864", "#EAF0F8")

# Abbreviations expanded as footnotes at the bottom of the email (only those that appear).
ABBREVIATIONS = {
    "UHealth": "University of Miami Health System",
    "UM": "University of Miami",
    "NIH": "National Institutes of Health",
    "CMS": "Centers for Medicare & Medicaid Services",
    "HHS": "U.S. Department of Health and Human Services",
    "FDA": "U.S. Food and Drug Administration",
    "CDC": "Centers for Disease Control and Prevention",
    "WHO": "World Health Organization",
    "340B": "the federal 340B Drug Pricing Program",
    "GME": "Graduate Medical Education",
    "PBM": "Pharmacy Benefit Manager",
    "AI": "Artificial Intelligence",
    "M&A": "Mergers & Acquisitions",
    "S&T": "Strategy & Transformation",
    "FIU": "Florida International University",
    "HCA": "Hospital Corporation of America",
    "ER": "Emergency Room",
    "EKG": "Electrocardiogram",
    "NCI": "National Cancer Institute",
    "ACA": "Affordable Care Act",
    "DRC": "Democratic Republic of the Congo",
    "COO": "Chief Operating Officer",
    "CMIO": "Chief Medical Information Officer",
    "CFO": "Chief Financial Officer",
    "CEO": "Chief Executive Officer",
    "DSH": "Disproportionate Share Hospital",
    "VA": "U.S. Department of Veterans Affairs",
    "STEMI": "ST-Elevation Myocardial Infarction",
    "RFI": "Request for Information",
    "GLP-1": "Glucagon-Like Peptide-1 (drug class)",
}


def _md_bold(text: str) -> str:
    """Escape HTML, then convert **strategic bolding** markers to <b> tags."""
    out = escape(str(text or ""))
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)


def _abbr_footnotes_html(blob: str) -> str:
    """Footnote block defining every known abbreviation that appears in `blob`."""
    found = []
    for ab, full in ABBREVIATIONS.items():
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(ab) + r"(?![A-Za-z0-9])", blob):
            found.append((ab, full))
    if not found:
        return ""
    found.sort(key=lambda x: x[0].lower())
    items = " &nbsp;·&nbsp; ".join(
        f"<b>{escape(ab)}</b> {escape(full)}" for ab, full in found)
    return (
        '<p style="color:#888;font-size:11px;line-height:1.5;margin:14px 0 0;'
        'border-top:1px solid #E6EBF2;padding-top:8px">'
        f'<b style="color:#1F3864">Abbreviations</b> &nbsp; {items}</p>'
    )


def render_html(briefing: dict, date_str: str, org_name: str, failing: list[str],
                greeting: str | None = None) -> str:
    def sec(title):
        return (f'<h2 style="color:#1F3864;font-size:11px;margin:9px 0 3px;'
                f'text-transform:uppercase;letter-spacing:.04em">{escape(title)}</h2>')

    stories = briefing.get("stories", [])

    # Overall relevance score — average of the selected stories' LLM relevance (0-10),
    # a quick read on how strongly prioritized the whole report is.
    nums = []
    for s in stories:
        try:
            nums.append(float(s.get("llm_score")))
        except (TypeError, ValueError):
            pass
    avg = sum(nums) / len(nums) if nums else None
    avg_html = (
        f'<span style="background:#1F3864;color:#fff;font-size:12px;font-weight:bold;'
        f'padding:2px 9px;border-radius:10px;white-space:nowrap">Report relevance '
        f'{avg:.1f}/10</span> <span style="color:#888;font-size:11px">avg of {len(nums)} '
        f'stories</span>'
    ) if avg is not None else ""

    # Group stories by intelligence area, and order the area sections by the day's priority
    # (the top story score in each area, high -> low). Within an area, sort by score.
    def _score(s):
        try:
            return float(s.get("llm_score"))
        except (TypeError, ValueError):
            return -1.0
    groups: dict[str, list] = {}
    for s in stories:
        groups.setdefault(s.get("area", ""), []).append(s)
    for g in groups.values():
        g.sort(key=_score, reverse=True)
    ordered_areas = sorted(groups, key=lambda a: max(_score(s) for s in groups[a]), reverse=True)

    parts = [
        '<div style="font-family:Arial,sans-serif;max-width:680px;margin:auto;color:#222;'
        'font-size:12.5px;line-height:1.28">',
        (f'<p style="font-size:12.5px;color:#222;margin:0 0 2px">Good morning {escape(greeting)},</p>'
         if greeting else ''),
        f'<h1 style="color:#1F3864;font-size:15px;margin:0 0 1px">Market Intelligence Briefing — '
        f'{escape(date_str)}</h1>',
        f'<p style="color:#666;font-size:10px;margin:0 0 4px">{escape(org_name)} · Highly '
        f'Confidential &nbsp; {avg_html}</p>',
    ]

    # Coverage snapshot — a color-coded index of the day's areas, highest priority first,
    # each with its story count. Replaces the old takeaways list.
    snap = []
    for area in ordered_areas:
        accent, tint = AREA_COLORS.get(area, DEFAULT_AREA_COLOR)
        snap.append(
            f'<span style="background:{tint};color:{accent};font-size:11px;font-weight:bold;'
            f'padding:1px 7px;border-radius:3px;white-space:nowrap;display:inline-block;'
            f'margin:0 4px 3px 0">{escape(AREA_LABELS.get(area, area))} &nbsp;{len(groups[area])}</span>'
        )
    parts.append(sec("Coverage snapshot"))
    parts.append('<p style="margin:0 0 3px">' + "".join(snap) + '</p>')

    fld = 'style="margin:1px 0;font-size:11.5px;line-height:1.25"'
    parts.append(sec("Today's Top Stories"))
    for s in sorted(stories, key=_score, reverse=True):
        area = s.get("area", "")
        accent, tint = AREA_COLORS.get(area, DEFAULT_AREA_COLOR)
        score = _fmt_score(s.get("llm_score"))
        score_badge = (
            f'<span style="background:#EAF0F8;color:#1F3864;font-size:11px;font-weight:bold;'
            f'padding:1px 7px;border-radius:10px;white-space:nowrap">Relevance {score}/10</span>'
        ) if score else ""
        chip = (
            f'<span style="background:{tint};color:{accent};font-size:10px;font-weight:bold;'
            f'padding:1px 6px;border-radius:3px;white-space:nowrap">'
            f'{escape(AREA_LABELS.get(area, area))}</span>'
        )
        next_steps = s.get("next_steps") or s.get("watch_next", "")
        parts.append(
            f'<div style="border-left:3px solid {accent};padding:3px 9px;margin:4px 0;'
            f'background:#F7F9FC">'
            f'<p style="margin:0 0 1px">{chip}&nbsp; {score_badge}'
            f'<span style="color:#888;font-size:10px">&nbsp; {escape(s.get("source", ""))}</span></p>'
            f'<p style="margin:0 0 1px"><b><a href="{escape(s.get("url", "#"))}" '
            f'style="color:#1F3864;text-decoration:none">{escape(s.get("title", ""))}</a></b></p>'
            f'<p {fld}><b>What happened:</b> {escape(s.get("what_happened", ""))}</p>'
            f'<p {fld}><b>Why it matters:</b> {escape(s.get("why_it_matters", ""))}</p>'
            f'<p {fld}><b>Exposure:</b> {escape(s.get("exposure", ""))}</p>'
            + (f'<p {fld}><b>Next steps:</b> {escape(next_steps)}</p>' if next_steps else "")
            + '</div>'
        )

    if failing:
        parts.append(
            f'<p style="color:#A33;font-size:11px;margin:8px 0 0">Source health alert — no items '
            f'for 2+ days: {escape(", ".join(failing))}</p>'
        )

    # Abbreviation footnotes — scan everything visible in the email body.
    blob = " ".join(
        f'{s.get("title","")} {s.get("what_happened","")} {s.get("why_it_matters","")} '
        f'{s.get("exposure","")} {s.get("next_steps","") or s.get("watch_next","")} '
        f'{AREA_LABELS.get(s.get("area",""), "")}'
        for s in stories
    )
    parts.append(_abbr_footnotes_html(blob))
    parts.append('<p style="color:#bbb;font-size:10px;margin:8px 0 0">Generated automatically by '
                 'the Market Intelligence Platform.</p></div>')
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
