"""HTML email rendering + SMTP delivery."""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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


def send(html: str, subject: str, smtp_cfg: dict):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["from"]
    msg["To"] = ", ".join(smtp_cfg["to"])
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg["port"])) as server:
        server.starttls()
        server.login(smtp_cfg["user"], smtp_cfg["password"])
        server.sendmail(smtp_cfg["from"], smtp_cfg["to"], msg.as_string())
