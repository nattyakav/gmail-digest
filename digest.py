#!/usr/bin/env python3
"""
Gmail Digest — AI-powered personal email digest tool.

Phase 2 : Connects to Gmail, fetches last 24 h of inbox emails,
           classifies + summarises each with Claude (claude-opus-4-6),
           and writes a beautiful HTML digest to output/digest.html.

Phase 3 : Sends a Telegram message with counts + a link to the digest
           (served by serve.py / flask_app.py).

Phase 4 : "Archive All" button in the HTML removes INBOX label from
           every email in the digest via the local/cloud server.
"""

import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Google ────────────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Anthropic ─────────────────────────────────────────────────────────────────
import anthropic

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
SCOPES     = ["https://www.googleapis.com/auth/gmail.modify"]
OUTPUT_DIR = Path("output")
PORT       = 8080
MODEL      = "claude-opus-4-6"
DIGEST_URL = os.getenv("DIGEST_URL", f"http://localhost:{PORT}").rstrip("/")

TIER_CONFIG: dict[int, dict] = {
    1: {"color": "#DC2626", "badge_bg": "#DC2626", "label": "Needs Action",   "emoji": "🔴"},
    2: {"color": "#D97706", "badge_bg": "#D97706", "label": "Worth Reading",  "emoji": "🟠"},
    3: {"color": "#059669", "badge_bg": "#059669", "label": "Low Priority",   "emoji": "🟢"},
}

# Pretty brand names for common sender domains
DOMAIN_NAMES: dict[str, str] = {
    "linkedin.com":      "LinkedIn",
    "github.com":        "GitHub",
    "gitlab.com":        "GitLab",
    "twitter.com":       "Twitter",
    "x.com":             "X (Twitter)",
    "facebook.com":      "Facebook",
    "instagram.com":     "Instagram",
    "youtube.com":       "YouTube",
    "tiktok.com":        "TikTok",
    "reddit.com":        "Reddit",
    "producthunt.com":   "Product Hunt",
    "amazon.com":        "Amazon",
    "amazon.co.il":      "Amazon IL",
    "ebay.com":          "eBay",
    "aliexpress.com":    "AliExpress",
    "paypal.com":        "PayPal",
    "stripe.com":        "Stripe",
    "google.com":        "Google",
    "gmail.com":         "Gmail",
    "googlemail.com":    "Gmail",
    "apple.com":         "Apple",
    "microsoft.com":     "Microsoft",
    "outlook.com":       "Outlook",
    "notion.so":         "Notion",
    "slack.com":         "Slack",
    "zoom.us":           "Zoom",
    "dropbox.com":       "Dropbox",
    "wix.com":           "Wix",
    "substack.com":      "Substack",
    "medium.com":        "Medium",
    "mailchimp.com":     "Mailchimp",
    "netflix.com":       "Netflix",
    "spotify.com":       "Spotify",
    "airbnb.com":        "Airbnb",
    "uber.com":          "Uber",
    "fiverr.com":        "Fiverr",
    "upwork.com":        "Upwork",
    "indeed.com":        "Indeed",
    "glassdoor.com":     "Glassdoor",
    "greenhouse.io":     "Greenhouse",
    "lever.co":          "Lever",
    "workable.com":      "Workable",
}

# ─────────────────────────────────────────────────────────────────────────────
# Claude prompts
# ─────────────────────────────────────────────────────────────────────────────

TAXONOMY = """\
TIER DEFINITIONS
────────────────
Tier 1 — Needs Action (reply / decision required):
  • Job offers or recruiter outreach
  • Press screening invitations or events requiring RSVP
  • Financial alerts that need immediate action (suspicious activity, payment overdue)
  • Direct personal outreach — someone writing *specifically to you* to collaborate,
    meet, work together, or connect personally (NOT automated / newsletter / promotional)

Tier 2 — Worth Reading (useful info, no reply needed):
  • Order confirmed / shipped / delayed / delivered
  • Regular financial statements or pension reports
  • Newsletters (all of them, even interesting ones)
  • Wix site or billing notices
  • General transactional emails

Tier 3 — Low Priority (skim or ignore):
  • GitHub notifications (hobby / personal repos)
  • Marketing, promotions, discount codes, sales campaigns
  • Pre-shipment shopping fluff ("your order is being prepared")
  • Social notifications (LinkedIn likes / follows, Twitter/X activity)
  • Automated system emails, app push notifications
"""

CLASSIFY_SYSTEM = f"""\
You are a personal email classifier and summariser.
{TAXONOMY}
Rules (apply strictly):
- Direct personal outreach written specifically to the recipient (not automated) → Tier 1
- Job offers, recruiters → Tier 1
- Newsletters → always Tier 2
- Marketing / promotions → always Tier 3
- Shipping confirmed/shipped/delayed/delivered → Tier 2
- "Being prepared" / pre-ship fluff → Tier 3

Respond with ONLY valid JSON, no markdown fences, no extra text:
{{"tier": 1|2|3, "category": "<short name, e.g. Job Offer>",
  "summary": "<2-3 clear sentences: what the email is about and what action if any is needed>",
  "is_newsletter": true|false}}"""

NEWSLETTER_DETAIL_SYSTEM = """\
You are summarising a newsletter. Respond with ONLY valid JSON, no markdown fences, no extra text.

Schema:
{
  "headline": "<one-line headline capturing the newsletter's main topic>",
  "sections": [
    {
      "title": "<short section title, 2-5 words>",
      "body":  "<1-2 paragraphs of flowing prose about this section, covering every important point>"
    }
  ]
}

Rules:
- Produce 2 to 4 sections that break the newsletter into logical topics.
- Each "body" is flowing prose in complete sentences. NO bullets, NO dashes, NO lists.
- Wrap key names, companies, or numbers with **double asterisks** for bold emphasis.
- Total prose across all sections: 200-400 words.
- Maximum 2 emojis across the entire response.
- Cover EVERY significant story or update in the newsletter."""

SUMMARISE_SYSTEM = "Summarise this email in 2–3 concise sentences."


# ─────────────────────────────────────────────────────────────────────────────
# Gmail helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds      = None
    token_path = Path("token.json")
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("  Refreshing Gmail token …")
            creds.refresh(Request())
        else:
            if not Path("credentials.json").exists():
                print("\nERROR: credentials.json not found. See SETUP instructions.")
                sys.exit(1)
            print("  Opening browser for Gmail sign-in …")
            flow  = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def get_emails_last_24h(service) -> list[dict]:
    cutoff = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp())
    query  = f"after:{cutoff} in:inbox"
    stubs, page_token = [], None
    while True:
        kwargs: dict = dict(userId="me", q=query, maxResults=100)
        if page_token:
            kwargs["pageToken"] = page_token
        result     = service.users().messages().list(**kwargs).execute()
        stubs     += result.get("messages", [])
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return stubs


# ── MIME parsing ──────────────────────────────────────────────────────────────

def _b64_decode(data: str) -> bytes:
    data = data.replace("-", "+").replace("_", "/")
    pad  = len(data) % 4
    if pad:
        data += "=" * (4 - pad)
    return base64.b64decode(data)


def _mime_text(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data", "")
    if mime == "text/plain" and data:
        return _b64_decode(data).decode("utf-8", errors="replace")
    if mime == "text/html" and data:
        html = _b64_decode(data).decode("utf-8", errors="replace")
        text = re.sub(r"<style[^>]*>.*?</style>",   " ", html, flags=re.DOTALL)
        text = re.sub(r"<script[^>]*>.*?</script>",  " ", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>",                    " ", text)
        return re.sub(r"\s+",                        " ", text).strip()
    plain_hit = html_hit = ""
    for part in payload.get("parts", []):
        candidate = _mime_text(part)
        if not candidate:
            continue
        if part.get("mimeType") == "text/plain":
            plain_hit = candidate
        else:
            html_hit = candidate
    return plain_hit or html_hit


def _first_image(service, msg_id: str, payload: dict) -> str | None:
    def search(part: dict) -> str | None:
        if part.get("mimeType", "").startswith("image/"):
            att_id = part.get("body", {}).get("attachmentId")
            if att_id:
                try:
                    att = service.users().messages().attachments().get(
                        userId="me", messageId=msg_id, id=att_id
                    ).execute()
                    raw = att.get("data", "")
                    if raw:
                        std = raw.replace("-", "+").replace("_", "/")
                        return f"data:{part['mimeType']};base64,{std}"
                except HttpError:
                    pass
        for sub in part.get("parts", []):
            result = search(sub)
            if result:
                return result
        return None
    return search(payload)


def _parse_list_unsubscribe(header: str) -> str:
    """Return first usable unsubscribe URL/mailto from a List-Unsubscribe header.
    Format is typically: <https://...>, <mailto:unsub@...>"""
    if not header:
        return ""
    # Extract values inside angle brackets
    candidates = re.findall(r"<([^>]+)>", header)
    # Prefer HTTPS, then HTTP, then mailto
    for pref in ("https://", "http://", "mailto:"):
        for c in candidates:
            if c.lower().startswith(pref):
                return c.strip()
    return candidates[0].strip() if candidates else ""


def get_email_details(service, msg_id: str) -> dict:
    msg     = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    sender_raw            = headers.get("From", "")
    display_name, address = parseaddr(sender_raw)
    domain                = address.split("@")[1].lower() if "@" in address else ""
    return {
        "id":           msg_id,
        "subject":      headers.get("Subject", "(no subject)"),
        "sender_name":  display_name or address,
        "sender_email": address,
        "sender_raw":   sender_raw,
        "domain":       domain,
        "date":         headers.get("Date", ""),
        "body":         _mime_text(msg["payload"])[:3000],
        "image":        _first_image(service, msg_id, msg["payload"]),
        # Gmail has no URL scheme for a threaded reply — "reply" and "open"
        # both land on the thread, where the user clicks Reply or presses R.
        "gmail_link":   f"https://mail.google.com/mail/u/0/#inbox/{msg_id}",
        "reply_link":   f"https://mail.google.com/mail/u/0/#inbox/{msg_id}",
        "unsubscribe":  _parse_list_unsubscribe(headers.get("List-Unsubscribe", "")),
    }


# Per-run cache to avoid duplicate Gmail queries for the same sender
_UNREAD_CACHE: dict[str, bool] = {}


def sender_all_unread_30d(service, sender_email: str) -> bool:
    """True if every message from sender in the last 30 days is unread.
    Uses a negative 'read' search — if zero results, none were read."""
    if not sender_email:
        return False
    if sender_email in _UNREAD_CACHE:
        return _UNREAD_CACHE[sender_email]
    try:
        q = f'from:{sender_email} newer_than:30d -is:unread'
        resp = service.users().messages().list(
            userId="me", q=q, maxResults=1
        ).execute()
        all_unread = not resp.get("messages")
    except HttpError:
        all_unread = False
    _UNREAD_CACHE[sender_email] = all_unread
    return all_unread


# ─────────────────────────────────────────────────────────────────────────────
# Claude helpers
# ─────────────────────────────────────────────────────────────────────────────

def _call_claude(client: anthropic.Anthropic, system: str, user: str,
                 max_tokens: int = 512) -> str:
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text.strip()


def _parse_json(raw: str) -> dict | None:
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
    raw = re.sub(r"\n?```$",       "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def classify_email(
    email: dict,
    vendor_tiers: dict,
    client: anthropic.Anthropic,
) -> tuple[int, str, str, bool]:
    """Return (tier, category, short_summary, is_newsletter)."""

    # ── Vendor override ──────────────────────────────────────────────────────
    for key, tier in vendor_tiers.items():
        if key.startswith("_"):
            continue
        k = key.lower()
        if k in email["sender_email"].lower() or k in email["domain"]:
            summary = _call_claude(
                client, SUMMARISE_SYSTEM,
                f"From: {email['sender_raw']}\nSubject: {email['subject']}\n\n"
                f"{email['body'][:1500]}",
                max_tokens=200,
            )
            return int(tier), "Vendor Override", summary, False

    # ── Claude classification ────────────────────────────────────────────────
    user_prompt = (
        f"From: {email['sender_raw']}\n"
        f"Subject: {email['subject']}\n\n"
        f"{email['body'][:2000]}"
    )
    raw    = _call_claude(client, CLASSIFY_SYSTEM, user_prompt)
    result = _parse_json(raw)

    if result:
        is_nl = bool(result.get("is_newsletter", False))
        return (
            int(result.get("tier", 3)),
            result.get("category", "General"),
            result.get("summary", "No summary available."),
            is_nl,
        )

    print(f"    ⚠  Unexpected Claude response: {raw[:100]}")
    return 3, "Uncategorized", "Summary unavailable.", False


def newsletter_full_summary(email: dict, client: anthropic.Anthropic) -> str:
    """Return structured HTML summary for a newsletter.
    Calls Claude for JSON, parses it, builds <h3>/<h4>/<p> directly.
    Falls back to a single <p> block on any failure."""
    user_prompt = (
        f"Newsletter from: {email['sender_raw']}\n"
        f"Subject: {email['subject']}\n\n"
        f"{email['body'][:3000]}"
    )
    try:
        raw    = _call_claude(client, NEWSLETTER_DETAIL_SYSTEM, user_prompt, max_tokens=900)
        data   = _parse_json(raw)
        if not data:
            return f"<p>{_esc(raw)}</p>"

        def fmt_inline(s: str) -> str:
            s = _esc(s)
            s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
            s = re.sub(r"\*(.+?)\*",     r"<em>\1</em>",          s)
            return s

        parts = []
        headline = data.get("headline", "").strip()
        if headline:
            parts.append(f"<h3>{fmt_inline(headline)}</h3>")

        for section in data.get("sections", []):
            title = (section.get("title") or "").strip()
            body  = (section.get("body")  or "").strip()
            if title:
                parts.append(f"<h4>{fmt_inline(title)}</h4>")
            # Split body into paragraphs on blank lines
            for para in re.split(r"\n{2,}", body):
                para = para.strip().replace("\n", " ")
                if para:
                    parts.append(f"<p>{fmt_inline(para)}</p>")

        return "\n".join(parts) if parts else f"<p>{_esc(raw)}</p>"

    except Exception as exc:
        print(f"    ⚠  Newsletter summary failed: {exc}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────

_EMAIL_PREFIX_RE = re.compile(
    r"^(mail|email|newsletter|noreply|no-reply|info|news|updates?"
    r"|notifications?|alerts?|support|hello|team|contact|do-not-reply|donotreply)\.",
    re.IGNORECASE,
)


def _pretty_domain(domain: str) -> str:
    """Return a human-friendly brand name for a sender domain."""
    if not domain:
        return "Unknown"
    clean = _EMAIL_PREFIX_RE.sub("", domain)
    for d in (clean, domain):
        if d in DOMAIN_NAMES:
            return DOMAIN_NAMES[d]
    parts = clean.split(".")
    return parts[-2].capitalize() if len(parts) >= 2 else clean.capitalize()


def _format_received(date_str: str) -> str:
    """Format the email Date header as a friendly time string."""
    if not date_str:
        return ""
    try:
        dt  = parsedate_to_datetime(date_str).astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        if delta.days == 0:
            return dt.strftime("%H:%M")
        if delta.days == 1:
            return f"Yesterday {dt.strftime('%H:%M')}"
        return dt.strftime("%b %#d")
    except Exception:
        return ""


def _esc(text: str) -> str:
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _avg_read_minutes(text: str) -> int:
    """Rough reading-time estimate at 200 wpm."""
    words = len(text.split())
    return max(1, round(words / 200))


def _md_to_html(text: str) -> str:
    """Convert markdown (# headings, **bold**, paragraphs) to safe HTML.
    Handles both single and double newline paragraph breaks."""

    def _inline(s: str) -> str:
        s = s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
        s = re.sub(r'\*(.+?)\*',     r'<em>\1</em>',          s)
        return s

    parts      = []
    para_lines: list[str] = []

    def flush():
        if para_lines:
            content = ' '.join(l for l in para_lines if l)
            if content:
                parts.append(f'<p>{_inline(content)}</p>')
            para_lines.clear()

    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            flush()
        elif line.startswith('#### ') or line.startswith('### '):
            flush()
            parts.append(f'<h4>{_inline(line.lstrip("#").strip())}</h4>')
        elif line.startswith('## '):
            flush()
            parts.append(f'<h4>{_inline(line[3:].strip())}</h4>')
        elif line.startswith('# '):
            flush()
            parts.append(f'<h3>{_inline(line[2:].strip())}</h3>')
        else:
            para_lines.append(line)

    flush()
    return '\n'.join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Card builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_card(email: dict, tier_num: int) -> str:
    cfg      = TIER_CONFIG[tier_num]
    domain   = email["domain"]
    brand    = _pretty_domain(domain)
    received = _format_received(email["date"])
    subject  = email["subject"][:160] + ("…" if len(email["subject"]) > 160 else "")
    is_nl    = email.get("is_newsletter", False)
    full_sum = email.get("full_summary", "")
    msg_id   = email["id"]

    favicon = (
        f'<img src="https://www.google.com/s2/favicons?domain={domain}&sz=64" '
        f'class="favicon" alt="" loading="lazy" '
        f'onerror="this.style.display=\'none\'">'
        if domain else ""
    )

    img_html = (
        f'<img src="{email["image"]}" alt="" class="card-img" loading="lazy">'
        if email.get("image") else ""
    )

    if is_nl and full_sum:
        # full_sum is already structured HTML from newsletter_full_summary()
        rt = _avg_read_minutes(re.sub(r"<[^>]+>", " ", full_sum))
        summary_html = f"""
        <p class="summary" id="short-{msg_id}">{_esc(email["summary"])}</p>
        <div class="full-s formatted-summary" id="full-{msg_id}" style="display:none">
          {full_sum}
        </div>
        <button class="expand-btn" id="btn-{msg_id}" onclick="toggleSummary('{msg_id}')">
          📖 Read full summary · ~{rt} min read
        </button>"""
    else:
        summary_html = f'<p class="summary">{_esc(email["summary"])}</p>'

    # Unsubscribe button — only for newsletters you haven't read in 30 days
    unsub_html = ""
    unsub_url  = email.get("unsubscribe", "")
    if is_nl and unsub_url and email.get("unread_30d"):
        unsub_html = (
            f'<a href="{_esc(unsub_url)}" target="_blank" rel="noopener" '
            f'class="unsub-link" title="You haven\'t opened any mail from this sender in 30 days">'
            f'🚫 Unsubscribe</a>'
        )

    search_str = _esc((brand + " " + email["sender_email"] + " " + subject + " " + email["summary"]).lower())

    return f"""
    <article class="email-card" style="border-left:5px solid {cfg["color"]};"
             data-search="{search_str}">

      <div class="card-header">
        <div class="brand-left">
          {favicon}
          <div class="brand-text">
            <span class="brand-name">{_esc(brand)}</span>
            <span class="sender-addr">{_esc(email["sender_email"])}</span>
          </div>
        </div>
        <div class="card-meta-right">
          <span class="received">{received}</span>
          <span class="tier-badge" style="background:{cfg["badge_bg"]};">{cfg["emoji"]} T{tier_num}</span>
        </div>
      </div>

      <div class="subject">{_esc(subject)}</div>

      {img_html}
      {summary_html}

      <div class="card-footer">
        <span class="cat-tag">{_esc(email["category"])}</span>
        <div class="action-links">
          {unsub_html}
          <a href="{email["reply_link"]}" target="_blank" rel="noopener" class="reply-link">↩ Reply</a>
          <a href="{email["gmail_link"]}" target="_blank" rel="noopener" class="open-link">Open in Gmail →</a>
        </div>
      </div>
    </article>"""


# ─────────────────────────────────────────────────────────────────────────────
# HTML page generator
# ─────────────────────────────────────────────────────────────────────────────

def generate_html(emails_by_tier: dict[int, list], date_str: str,
                  base_url: str = "http://localhost:8080") -> str:

    total       = sum(len(v) for v in emails_by_tier.values())
    t1, t2, t3 = (
        len(emails_by_tier.get(1, [])),
        len(emails_by_tier.get(2, [])),
        len(emails_by_tier.get(3, [])),
    )
    time_saved = total * 3

    sections_html = ""
    for tier_num in [1, 2, 3]:
        emails = emails_by_tier.get(tier_num, [])
        if not emails:
            continue
        cfg         = TIER_CONFIG[tier_num]
        cards       = "".join(_build_card(e, tier_num) for e in emails)
        count_label = f'{len(emails)} email{"s" if len(emails) != 1 else ""}'
        sections_html += f"""
    <section class="tier-section" data-tier="{tier_num}">
      <div class="tier-header">
        <span class="tier-dot" style="background:{cfg["color"]};"></span>
        <h2 style="color:{cfg["color"]};">{cfg["emoji"]} Tier {tier_num} — {cfg["label"]}</h2>
        <span class="count-badge">{count_label}</span>
      </div>
      <div class="cards-list">{cards}</div>
    </section>"""

    if not sections_html:
        sections_html = """
    <div class="empty-state">
      <div class="empty-icon">📭</div>
      <h3>Inbox is clear</h3>
      <p>No emails found in the last 24 hours.</p>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Gmail Digest — {date_str}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,400;0,700;0,900;1,400&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing:border-box; margin:0; padding:0; }}

    body {{
      font-family:'Lato',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
      background:#F0F4F8; color:#0F172A; min-height:100vh; padding-bottom:120px;
    }}

    /* ── Header ────────────────────────────────── */
    .digest-header {{
      background:linear-gradient(135deg,#1E3A8A 0%,#2563EB 60%,#3B82F6 100%);
      color:#fff; padding:40px 48px 32px; text-align:center;
    }}
    .header-top {{
      display:flex; align-items:center; justify-content:center;
      gap:16px; margin-bottom:6px; position:relative;
    }}
    .digest-header h1 {{
      font-size:clamp(26px,4vw,38px); font-weight:800;
      letter-spacing:-.5px;
    }}
    .settings-link {{
      position:absolute; right:0; top:50%; transform:translateY(-50%);
      color:rgba(255,255,255,.75); font-size:14px; font-weight:600;
      text-decoration:none; background:rgba(255,255,255,.15);
      padding:7px 16px; border-radius:20px;
      border:1px solid rgba(255,255,255,.25);
      transition:background .2s;
    }}
    .settings-link:hover {{ background:rgba(255,255,255,.25); color:#fff; }}
    .subtitle {{ font-size:14px; opacity:.75; margin-bottom:28px; }}

    /* ── Stats ── */
    .stats-bar {{
      display:flex; justify-content:center; gap:12px;
      flex-wrap:wrap; margin-bottom:24px;
    }}
    .stat-pill {{
      background:rgba(255,255,255,.15); backdrop-filter:blur(6px);
      border:1px solid rgba(255,255,255,.2); border-radius:40px;
      padding:10px 22px; text-align:center; min-width:92px;
    }}
    .stat-pill.total {{ background:rgba(255,255,255,.28); }}
    .stat-pill.t1    {{ background:rgba(220,38,38,.35);   border-color:rgba(220,38,38,.5); }}
    .stat-pill.t2    {{ background:rgba(217,119,6,.35);   border-color:rgba(217,119,6,.5); }}
    .stat-pill.t3    {{ background:rgba(5,150,105,.35);   border-color:rgba(5,150,105,.5); }}
    .stat-pill.saved {{ background:rgba(255,255,255,.12); min-width:130px; }}
    .stat-num {{ font-size:26px; font-weight:800; line-height:1; }}
    .stat-lbl {{ font-size:11px; opacity:.8; margin-top:3px; text-transform:uppercase; letter-spacing:.5px; }}

    /* ── Search ── */
    .search-wrap {{
      position:relative; max-width:560px; margin:0 auto;
    }}
    .search-icon {{
      position:absolute; left:16px; top:50%; transform:translateY(-50%);
      font-size:16px; pointer-events:none;
    }}
    .search-box {{
      width:100%; padding:13px 20px 13px 46px;
      border-radius:32px; border:none; font-size:15px;
      background:rgba(255,255,255,.2); color:#fff;
      outline:none; backdrop-filter:blur(4px);
      transition:background .2s;
    }}
    .search-box::placeholder {{ color:rgba(255,255,255,.6); }}
    .search-box:focus {{ background:rgba(255,255,255,.3); }}

    /* ── Layout ── */
    .container {{ max-width:1100px; margin:0 auto; padding:32px 32px; }}

    /* ── Tier section ── */
    .tier-section {{ margin-bottom:48px; }}
    .tier-header {{
      display:flex; align-items:center; gap:12px;
      margin-bottom:16px; padding-bottom:12px;
      border-bottom:2px solid #E2E8F0;
    }}
    .tier-dot   {{ width:13px; height:13px; border-radius:50%; flex-shrink:0; }}
    .tier-header h2 {{ font-size:18px; font-weight:700; flex:1; }}
    .count-badge {{
      font-size:13px; color:#64748B; background:#E2E8F0;
      padding:4px 14px; border-radius:20px; font-weight:600;
    }}

    /* ── Cards list — single column, full width ── */
    .cards-list {{
      display:flex; flex-direction:column; gap:12px;
    }}

    /* ── Card ── */
    .email-card {{
      background:#fff; border-radius:18px;
      box-shadow:0 2px 6px rgba(0,0,0,.08);
      transition:box-shadow .2s, transform .15s;
      padding:28px 40px;
      border-left:6px solid transparent; /* colour set inline */
    }}
    .email-card:hover {{
      box-shadow:0 6px 28px rgba(0,0,0,.11);
      transform:translateY(-2px);
    }}
    .email-card.hidden {{ display:none; }}

    /* ── Card header row ── */
    .card-header {{
      display:flex; align-items:center;
      justify-content:space-between; gap:16px;
      margin-bottom:14px;
    }}
    .brand-left {{
      display:flex; align-items:center; gap:14px; flex:1; min-width:0;
    }}
    .favicon {{
      width:46px; height:46px; border-radius:11px; flex-shrink:0;
      object-fit:contain; background:#F8FAFC; padding:3px;
    }}
    .brand-text {{ min-width:0; }}
    .brand-name {{
      display:block; font-size:22px; font-weight:900;
      letter-spacing:-.4px; line-height:1.2;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
      color:#0F172A;
    }}
    .sender-addr {{
      display:block; font-size:14px; color:#94A3B8; margin-top:3px;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    }}
    .card-meta-right {{
      display:flex; align-items:center; gap:10px; flex-shrink:0;
    }}
    .received   {{ font-size:14px; color:#94A3B8; white-space:nowrap; font-weight:700; }}
    .tier-badge {{
      color:#fff; font-size:13px; font-weight:700;
      padding:6px 14px; border-radius:20px;
      white-space:nowrap; letter-spacing:.3px;
    }}

    /* ── Subject ── */
    .subject {{
      font-size:21px; font-weight:700; color:#1E293B;
      line-height:1.4; margin-bottom:14px;
    }}

    /* ── Summary ── */
    .summary {{
      font-size:16px; color:#475569; line-height:1.8;
      margin-bottom:4px;
    }}
    .full-text {{
      border-top:1px solid #F1F5F9; padding-top:14px; margin-top:8px;
    }}

    /* ── Formatted newsletter summary ── */
    .formatted-summary {{
      border-top:1px solid #F1F5F9;
      padding-top:18px; margin-top:10px;
    }}
    .formatted-summary h3 {{
      font-size:18px; font-weight:900; color:#1E293B;
      letter-spacing:-.3px; line-height:1.3;
      margin:0 0 14px;
    }}
    .formatted-summary h4 {{
      font-size:15px; font-weight:700; color:#334155;
      letter-spacing:-.1px; margin:20px 0 8px;
      padding-bottom:4px; border-bottom:1px solid #F1F5F9;
    }}
    .formatted-summary p {{
      font-size:15px; color:#475569; line-height:1.85;
      margin:0 0 12px;
    }}
    .formatted-summary p:last-child {{ margin-bottom:0; }}
    .formatted-summary strong {{ color:#1E293B; font-weight:700; }}
    .formatted-summary em {{ color:#64748B; font-style:italic; }}

    /* ── Card image ── */
    .card-img {{
      width:100%; max-height:240px; object-fit:cover;
      border-radius:10px; margin:12px 0; display:block;
    }}

    /* ── Expand / collapse (newsletters) ── */
    .expand-btn {{
      display:inline-block; margin:10px 0 4px;
      background:none; border:1.5px solid #CBD5E1;
      color:#2563EB; font-size:15px; font-weight:700;
      padding:10px 24px; border-radius:24px; cursor:pointer;
      font-family:'Lato',sans-serif;
      transition:background .15s, border-color .15s;
    }}
    .expand-btn:hover {{ background:#EFF6FF; border-color:#93C5FD; }}

    /* ── Card footer ── */
    .card-footer {{
      display:flex; align-items:center; justify-content:space-between;
      gap:12px; padding-top:18px; margin-top:18px;
      border-top:1px solid #F1F5F9;
    }}
    .cat-tag {{
      font-size:13px; color:#64748B; background:#F1F5F9;
      padding:5px 14px; border-radius:14px; font-weight:700;
      white-space:nowrap;
    }}
    .action-links {{ display:flex; align-items:center; gap:16px; }}
    .reply-link, .open-link {{
      font-size:15px; font-weight:700; text-decoration:none; white-space:nowrap;
    }}
    .reply-link {{ color:#64748B; }}
    .reply-link:hover {{ color:#0F172A; }}
    .open-link  {{ color:#2563EB; }}
    .open-link:hover {{ text-decoration:underline; }}

    /* ── Empty state ── */
    .empty-state {{ text-align:center; padding:80px 20px; color:#94A3B8; }}
    .empty-icon  {{ font-size:60px; margin-bottom:20px; }}
    .empty-state h3 {{ font-size:22px; margin-bottom:10px; color:#64748B; }}
    .empty-state p  {{ font-size:15px; }}

    /* ── No-results ── */
    .no-results {{
      text-align:center; padding:40px 20px; color:#94A3B8;
      font-size:16px; display:none;
    }}

    /* ── Archive bar ── */
    .archive-bar {{
      position:fixed; bottom:0; left:0; right:0;
      background:rgba(255,255,255,.97); backdrop-filter:blur(14px);
      border-top:1px solid #E2E8F0; padding:14px 32px;
      display:flex; align-items:center; justify-content:center; gap:18px;
      z-index:100; box-shadow:0 -4px 28px rgba(0,0,0,.09);
    }}
    .archive-btn {{
      background:#1E3A8A; color:#fff; border:none;
      padding:12px 30px; border-radius:12px;
      font-size:15px; font-weight:700; cursor:pointer;
      transition:background .2s; letter-spacing:.2px;
    }}
    .archive-btn:hover:not(:disabled) {{ background:#1D4ED8; }}
    .archive-btn:disabled {{ opacity:.5; cursor:not-allowed; }}
    .archive-status         {{ font-size:14px; color:#64748B; }}
    .archive-status.success {{ color:#059669; font-weight:600; }}
    .archive-status.error   {{ color:#DC2626; }}

    /* ── Run status bar ── */
    .run-status-bar {{
      display:none; /* shown by JS after fetch */
      max-width:640px; margin:18px auto 0;
      padding:10px 20px; border-radius:24px;
      font-size:14px; font-weight:700; letter-spacing:.1px;
      backdrop-filter:blur(6px);
    }}
    .run-status-bar.ok    {{ background:rgba(5,150,105,.25); border:1px solid rgba(5,150,105,.4); }}
    .run-status-bar.err   {{ background:rgba(220,38,38,.25);  border:1px solid rgba(220,38,38,.5); }}
    .run-status-bar.run   {{ background:rgba(255,255,255,.2); border:1px solid rgba(255,255,255,.3); }}

    /* ── Mode toggle button ── */
    .mode-toggle {{
      position:absolute; left:0; top:50%; transform:translateY(-50%);
      background:rgba(255,255,255,.15); border:1px solid rgba(255,255,255,.25);
      color:#fff; width:38px; height:38px; border-radius:50%;
      font-size:18px; cursor:pointer; transition:background .2s;
      display:flex; align-items:center; justify-content:center;
    }}
    .mode-toggle:hover {{ background:rgba(255,255,255,.3); }}

    /* ── Unsubscribe link ── */
    .unsub-link {{
      color:#B45309; font-size:13px; font-weight:600;
      text-decoration:none; padding:4px 10px;
      background:#FEF3C7; border:1px solid #FDE68A;
      border-radius:12px; transition:background .15s;
    }}
    .unsub-link:hover {{ background:#FDE68A; }}

    /* ── Dark mode overrides ── */
    body.dark {{
      background:#0B1220; color:#E2E8F0;
    }}
    body.dark .digest-header {{
      background:linear-gradient(135deg,#0B1220 0%,#1E293B 60%,#334155 100%);
    }}
    body.dark .email-card {{
      background:#1E293B; color:#E2E8F0;
      box-shadow:0 1px 3px rgba(0,0,0,.4);
    }}
    body.dark .brand-name {{ color:#F1F5F9; }}
    body.dark .sender-addr,
    body.dark .received,
    body.dark .cat-tag {{ color:#94A3B8; }}
    body.dark .subject {{ color:#F8FAFC; }}
    body.dark .summary {{ color:#CBD5E1; }}
    body.dark .full-s p,
    body.dark .full-s h3,
    body.dark .full-s h4 {{ color:#E2E8F0; }}
    body.dark .formatted-summary strong {{ color:#F8FAFC; }}
    body.dark .formatted-summary em     {{ color:#94A3B8; }}
    body.dark .expand-btn {{
      background:#0B1220; border-color:#334155; color:#93C5FD;
    }}
    body.dark .expand-btn:hover {{ background:#1E293B; border-color:#60A5FA; }}
    body.dark .cat-tag {{ background:#334155; }}
    body.dark .reply-link,
    body.dark .open-link {{ color:#93C5FD; }}
    body.dark .unsub-link {{
      color:#FCD34D; background:rgba(251,191,36,.15);
      border-color:rgba(251,191,36,.3);
    }}
    body.dark .unsub-link:hover {{ background:rgba(251,191,36,.25); }}
    body.dark .tier-header h2 {{ filter:brightness(1.3); }}
    body.dark .count-badge {{
      background:#1E293B; color:#CBD5E1; border:1px solid #334155;
    }}
    body.dark .search-box {{
      background:rgba(255,255,255,.1); color:#fff;
      border-color:rgba(255,255,255,.2);
    }}
    body.dark .no-results,
    body.dark .empty-state h3 {{ color:#CBD5E1; }}
    body.dark .archive-bar {{
      background:#1E293B; border-top:1px solid #334155;
    }}
    body.dark .archive-status {{ color:#CBD5E1; }}

    /* ── Mobile ── */
    @media(max-width:700px) {{
      .digest-header {{ padding:28px 20px 24px; }}
      .settings-link {{ position:static; transform:none; margin-top:12px; display:inline-block; }}
      .mode-toggle   {{ position:static; transform:none; margin-top:12px; }}
      .header-top {{ flex-wrap:wrap; justify-content:center; }}
      .container  {{ padding:20px 16px; }}
      .email-card {{ padding:20px 20px; }}
      .brand-name {{ font-size:18px; }}
      .subject    {{ font-size:17px; }}
      .summary    {{ font-size:15px; }}
      .archive-bar {{ flex-direction:column; gap:8px; padding:12px 16px; }}
    }}
  </style>
</head>
<body>

  <header class="digest-header">
    <div class="header-top">
      <button class="mode-toggle" id="modeToggle" onclick="toggleDark()" title="Toggle dark mode">🌙</button>
      <h1>📬 Gmail Digest</h1>
      <a href="{base_url}/settings" class="settings-link">⚙ Settings</a>
    </div>
    <div class="subtitle">{date_str}</div>
    <div class="stats-bar">
      <div class="stat-pill total">
        <div class="stat-num">{total}</div><div class="stat-lbl">Total</div>
      </div>
      <div class="stat-pill t1">
        <div class="stat-num">{t1}</div><div class="stat-lbl">Action</div>
      </div>
      <div class="stat-pill t2">
        <div class="stat-num">{t2}</div><div class="stat-lbl">Reading</div>
      </div>
      <div class="stat-pill t3">
        <div class="stat-num">{t3}</div><div class="stat-lbl">Low Pri</div>
      </div>
      <div class="stat-pill saved">
        <div class="stat-num">~{time_saved}m</div><div class="stat-lbl">Time Saved</div>
      </div>
    </div>
    <div class="search-wrap">
      <span class="search-icon">🔍</span>
      <input class="search-box" type="text" placeholder="Search emails…"
             oninput="filterCards(this.value)" autocomplete="off">
    </div>
    <div class="run-status-bar" id="runStatusBar"></div>
  </header>

  <main class="container">
    {sections_html}
    <p class="no-results" id="noResults">No emails match your search.</p>
  </main>

  <div class="archive-bar">
    <button class="archive-btn" id="archiveBtn" onclick="archiveAll()">
      🗂 Archive All Emails
    </button>
    <div class="archive-status" id="archiveStatus"></div>
  </div>

  <script>
    /* ── Dark mode (persisted in localStorage) ── */
    (function() {{
      if (localStorage.getItem('digestDarkMode') === '1') {{
        document.body.classList.add('dark');
        const btn = document.getElementById('modeToggle');
        if (btn) btn.textContent = '☀️';
      }}
    }})();
    function toggleDark() {{
      const on = document.body.classList.toggle('dark');
      localStorage.setItem('digestDarkMode', on ? '1' : '0');
      const btn = document.getElementById('modeToggle');
      if (btn) btn.textContent = on ? '☀️' : '🌙';
    }}

    /* ── Run status ── */
    (async function() {{
      try {{
        const res = await fetch('{base_url}/status');
        if (!res.ok) return;
        const d = await res.json();
        const bar = document.getElementById('runStatusBar');
        if (!bar || !d.status) return;

        function fmtTime(iso) {{
          if (!iso) return '';
          const dt = new Date(iso);
          return dt.toLocaleDateString(undefined, {{weekday:'short',month:'short',day:'numeric'}})
               + ' · ' + dt.toLocaleTimeString(undefined, {{hour:'2-digit',minute:'2-digit'}});
        }}

        if (d.status === 'success') {{
          bar.textContent = '✅  Last run: ' + fmtTime(d.finished_at)
            + '  ·  ' + (d.total || 0) + ' emails processed';
          bar.className = 'run-status-bar ok';
        }} else if (d.status === 'failed') {{
          bar.textContent = '❌  Last run failed  ·  ' + (d.error || 'Unknown error');
          bar.className = 'run-status-bar err';
        }} else if (d.status === 'running') {{
          bar.textContent = '⏳  Digest is running right now…';
          bar.className = 'run-status-bar run';
        }}
        bar.style.display = 'block';
      }} catch(e) {{ /* server offline — silently skip */ }}
    }})();

    /* ── Expand / collapse newsletter summary ── */
    function toggleSummary(id) {{
      const full = document.getElementById('full-' + id);
      const btn  = document.getElementById('btn-'  + id);
      const open = full.style.display !== 'none';
      full.style.display = open ? 'none' : 'block';
      btn.textContent    = open ? btn.dataset.orig : '▲ Collapse summary';
    }}
    document.querySelectorAll('.expand-btn').forEach(b => {{
      b.dataset.orig = b.textContent;
    }});

    /* ── Live search ── */
    function filterCards(query) {{
      const q        = query.trim().toLowerCase();
      const cards    = document.querySelectorAll('.email-card');
      const sections = document.querySelectorAll('.tier-section');
      let   anyVis   = false;
      cards.forEach(c => {{
        const match = !q || c.dataset.search.includes(q);
        c.classList.toggle('hidden', !match);
        if (match) anyVis = true;
      }});
      sections.forEach(s => {{
        const vis = [...s.querySelectorAll('.email-card')]
          .some(c => !c.classList.contains('hidden'));
        s.style.display = vis ? '' : 'none';
      }});
      document.getElementById('noResults').style.display =
        anyVis ? 'none' : 'block';
    }}

    /* ── Archive all ── */
    async function archiveAll() {{
      const btn    = document.getElementById('archiveBtn');
      const status = document.getElementById('archiveStatus');
      btn.disabled       = true;
      btn.textContent    = 'Archiving…';
      status.textContent = '';
      status.className   = 'archive-status';
      try {{
        const res  = await fetch('{base_url}/archive', {{ method:'POST' }});
        const data = await res.json();
        if (data.success) {{
          const n = data.archived;

          // Fade out every email card
          document.querySelectorAll('.email-card').forEach(card => {{
            card.style.transition = 'opacity 0.35s, transform 0.35s';
            card.style.opacity    = '0';
            card.style.transform  = 'translateY(-6px)';
          }});

          // After the fade, clear the DOM and show a done state
          setTimeout(() => {{
            document.querySelectorAll('.tier-section').forEach(s => s.remove());
            document.getElementById('noResults').style.display = 'none';

            const main = document.querySelector('.container');
            main.innerHTML = `
              <div class="empty-state" style="margin-top:60px">
                <div class="empty-icon">✅</div>
                <h3>All done!</h3>
                <p>${{n}} email${{n !== 1 ? 's' : ''}} archived — inbox cleared.</p>
              </div>`;

            // Hide the archive bar too
            document.querySelector('.archive-bar').style.display = 'none';
          }}, 400);

        }} else {{ throw new Error(data.error || 'Unknown error'); }}
      }} catch (e) {{
        btn.textContent    = '🗂 Archive All Emails';
        btn.disabled       = false;
        status.textContent = 'Failed: ' + e.message;
        status.className   = 'archive-status error';
      }}
    }}
  </script>

</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Telegram  (Phase 3)
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(counts: dict[int, int], total: int) -> None:
    import urllib.request as urlreq
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        print("  ⚠  TELEGRAM credentials not found in .env — skipping.")
        return
    t1, t2, t3 = counts.get(1, 0), counts.get(2, 0), counts.get(3, 0)
    auth_token = os.getenv("DIGEST_AUTH_TOKEN", "").strip()
    digest_link = f"{DIGEST_URL}?t={auth_token}" if auth_token else DIGEST_URL
    text = (
        f"📬 *Gmail Digest is ready!*\n\n"
        f"📊 *{total}* email{'s' if total != 1 else ''} in the last 24 hours:\n"
        f"🔴 Needs Action:  *{t1}*\n"
        f"🟠 Worth Reading: *{t2}*\n"
        f"🟢 Low Priority:  *{t3}*\n\n"
        f"👉 [Open Digest]({digest_link})"
    )
    payload = json.dumps({"chat_id": chat_id, "text": text,
                           "parse_mode": "Markdown"}).encode("utf-8")
    req = urlreq.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"},
    )
    try:
        urlreq.urlopen(req, timeout=10)
        print("  ✅ Telegram notification sent!")
    except Exception as exc:
        print(f"  ⚠  Telegram failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _write_run_log(status: str, error: str | None = None,
                   counts: dict | None = None, total: int = 0,
                   started_at: str = "") -> None:
    """Write run_log.json so the digest page can show last-run status."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    log = {
        "status":      status,           # "running" | "success" | "failed"
        "started_at":  started_at,
        "finished_at": datetime.now().isoformat() if status != "running" else None,
        "total":       total,
        "counts":      counts or {},
        "error":       error,
    }
    (OUTPUT_DIR / "run_log.json").write_text(
        json.dumps(log, indent=2), encoding="utf-8"
    )


def _friendly_api_error(exc: Exception) -> str:
    """Return a human-readable message for Anthropic API errors."""
    msg = str(exc).lower()
    if "credit" in msg or "quota" in msg or "billing" in msg or "balance" in msg:
        return ("Anthropic API quota exceeded — add credits at "
                "console.anthropic.com/settings/billing")
    if "401" in msg or "authentication" in msg or "api_key" in msg:
        return "Anthropic API key is invalid or expired — check .env"
    if "529" in msg or "overload" in msg:
        return "Anthropic API is overloaded — will retry on next run"
    return f"Anthropic API error: {exc}"


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key or api_key == "your_anthropic_api_key_here":
        print("ERROR: ANTHROPIC_API_KEY not set. Edit .env and add your key.")
        _write_run_log("failed", error="ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    started_at = datetime.now().isoformat()
    _write_run_log("running", started_at=started_at)

    try:
        _main_inner(api_key, started_at)
    except anthropic.APIStatusError as exc:
        msg = _friendly_api_error(exc)
        print(f"\n❌ {msg}")
        _write_run_log("failed", error=msg, started_at=started_at)
        sys.exit(1)
    except anthropic.APIConnectionError as exc:
        msg = f"Cannot reach Anthropic API — check internet connection ({exc})"
        print(f"\n❌ {msg}")
        _write_run_log("failed", error=msg, started_at=started_at)
        sys.exit(1)
    except Exception as exc:
        msg = str(exc)
        print(f"\n❌ Unexpected error: {msg}")
        _write_run_log("failed", error=msg, started_at=started_at)
        raise


def _main_inner(api_key: str, started_at: str) -> None:
    ai_client    = anthropic.Anthropic(api_key=api_key)
    vendor_tiers = json.loads(Path("vendor_tiers.json").read_text(encoding="utf-8"))

    # ── Load ignored domains ─────────────────────────────────────────────────
    ignored_path    = Path("ignored_domains.json")
    ignored_domains: set[str] = set()
    if ignored_path.exists():
        ignored_domains = set(json.loads(ignored_path.read_text(encoding="utf-8")))
    if ignored_domains:
        print(f"🚫 Ignoring {len(ignored_domains)} domain(s): {', '.join(sorted(ignored_domains))}")

    print("\n🔐 Authenticating with Gmail …")
    service = get_gmail_service()

    print("📥 Fetching emails from the last 24 hours …")
    stubs = get_emails_last_24h(service)

    if not stubs:
        print("📭 No emails found in the last 24 hours.")
        _write_run_log("success", counts={"1":0,"2":0,"3":0}, total=0,
                       started_at=started_at)
        return

    print(f"📧 {len(stubs)} emails found. Classifying with {MODEL} …\n")

    emails_by_tier: dict[int, list] = {1: [], 2: [], 3: []}
    all_ids:        list[str]       = []
    domain_counts:  dict[str, int]  = {}

    for i, stub in enumerate(stubs, 1):
        msg_id = stub["id"]
        try:
            email = get_email_details(service, msg_id)
            label = email["subject"][:55] + ("…" if len(email["subject"]) > 55 else "")
            print(f"  [{i:>3}/{len(stubs)}] {label}")

            d = email["domain"]
            if d:
                domain_counts[d] = domain_counts.get(d, 0) + 1

            if d in ignored_domains:
                print(f"         → Skipped (ignored in Settings)")
                continue

            all_ids.append(msg_id)

            tier, category, summary, is_nl = classify_email(
                email, vendor_tiers, ai_client
            )
            email.update(tier=tier, category=category,
                         summary=summary, is_newsletter=is_nl)

            if is_nl:
                print(f"         → Newsletter — generating full summary …")
                email["full_summary"] = newsletter_full_summary(email, ai_client)
                # Unsubscribe helper: check 30-day read status only if we have a link
                if email.get("unsubscribe"):
                    email["unread_30d"] = sender_all_unread_30d(
                        service, email["sender_email"]
                    )
            else:
                email["full_summary"] = ""

            emails_by_tier[tier].append(email)

        except anthropic.APIStatusError:
            raise   # bubble up to main() for proper error logging
        except Exception as exc:
            print(f"    ⚠  Error processing {stub['id']}: {exc}")

    # ── Generate HTML ────────────────────────────────────────────────────────
    now      = datetime.now()
    date_str = now.strftime(f"%A, %B {now.day}, %Y")
    html     = generate_html(emails_by_tier, date_str, DIGEST_URL)

    html_path = OUTPUT_DIR / "digest.html"
    meta_path = OUTPUT_DIR / "digest_meta.json"

    html_path.write_text(html, encoding="utf-8")

    domains_list = [
        {"domain": d, "brand": _pretty_domain(d), "count": c}
        for d, c in sorted(domain_counts.items(), key=lambda x: -x[1])
    ]
    meta = {
        "email_ids":    all_ids,
        "generated_at": now.isoformat(),
        "counts":       {str(k): len(v) for k, v in emails_by_tier.items()},
        "domains":      domains_list,
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # ── Cumulative all-time domain history ───────────────────────────────────
    # Merge today's domains into a persistent record so the settings page
    # shows every sender ever seen, not just today's.
    all_domains_path = Path("all_domains.json")
    try:
        history = {d["domain"]: d for d in
                   json.loads(all_domains_path.read_text(encoding="utf-8"))}
    except (FileNotFoundError, json.JSONDecodeError):
        history = {}

    now_iso = now.isoformat()
    for domain, count in domain_counts.items():
        entry = history.get(domain, {})
        history[domain] = {
            "domain":      domain,
            "brand":       _pretty_domain(domain),
            "first_seen":  entry.get("first_seen", now_iso),
            "last_seen":   now_iso,
            "total_count": entry.get("total_count", 0) + count,
        }
    # Sort by last_seen desc so most-recent domains appear first
    sorted_history = sorted(
        history.values(), key=lambda x: x.get("last_seen", ""), reverse=True
    )
    all_domains_path.write_text(
        json.dumps(sorted_history, indent=2), encoding="utf-8"
    )

    counts = {k: len(v) for k, v in emails_by_tier.items()}
    total  = sum(counts.values())

    _write_run_log("success",
                   counts={str(k): v for k, v in counts.items()},
                   total=total, started_at=started_at)

    print(f"\n✅ Digest saved → {html_path.resolve()}")
    print(
        f"   🔴 Tier 1: {counts[1]}  "
        f"🟠 Tier 2: {counts[2]}  "
        f"🟢 Tier 3: {counts[3]}"
    )

    print("\n📲 Sending Telegram notification …")
    send_telegram(counts, total)

    print(f"\n🌐 Run the server:  python serve.py")
    print(f"   Then open:       {DIGEST_URL}\n")


if __name__ == "__main__":
    main()
