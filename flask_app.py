"""
flask_app.py — PythonAnywhere web app (always-on).

  GET  /          Serves the latest output/digest.html
  POST /archive   Removes INBOX label from all digest emails (Phase 4)

PythonAnywhere WSGI config must point to this file's `app` object.
"""

import html
import json
import os
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, request, send_file

# Anchor all paths to the directory this file lives in
BASE_DIR   = Path(__file__).parent.resolve()
OUTPUT_DIR = BASE_DIR / "output"

load_dotenv(BASE_DIR / ".env")

# ── Google ────────────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _gmail_service():
    token_path = BASE_DIR / "token.json"
    if not token_path.exists():
        raise RuntimeError("token.json not found")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

AUTH_TOKEN = os.getenv("DIGEST_AUTH_TOKEN", "").strip()


def require_auth(f):
    """Gate endpoint behind DIGEST_AUTH_TOKEN.
    Accepts token via ?t= query param, X-Auth header, or digest_auth cookie.
    When passed via ?t=, the token is persisted as a cookie so future requests
    (same-origin fetches from the HTML) auto-authenticate. If the env var is
    not set (e.g. local dev), auth is skipped."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not AUTH_TOKEN:
            return f(*args, **kwargs)
        if request.method == "OPTIONS":
            return f(*args, **kwargs)
        provided = (
            request.args.get("t")
            or request.cookies.get("digest_auth")
            or request.headers.get("X-Auth")
        )
        if provided != AUTH_TOKEN:
            return ("Unauthorized", 401)
        resp = make_response(f(*args, **kwargs))
        if request.args.get("t") == AUTH_TOKEN:
            resp.set_cookie(
                "digest_auth", AUTH_TOKEN,
                max_age=60 * 60 * 24 * 365,
                httponly=True, secure=True, samesite="Lax",
            )
        return resp
    return wrapper


@app.route("/", methods=["GET"])
@require_auth
def index():
    digest = OUTPUT_DIR / "digest.html"
    if not digest.exists():
        return (
            "<h2 style='font-family:sans-serif;padding:40px'>"
            "📭 Digest not ready yet — check back after 09:00 TLV.</h2>",
            404,
        )
    return send_file(str(digest))


@app.route("/archive", methods=["POST", "OPTIONS"])
@require_auth
def archive():
    if request_is_preflight():
        return jsonify({}), 204

    meta_path = OUTPUT_DIR / "digest_meta.json"
    if not meta_path.exists():
        return jsonify({"success": False,
                        "error": "No metadata. Run digest.py first."}), 400

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    ids  = meta.get("email_ids", [])

    if not ids:
        return jsonify({"success": True, "archived": 0,
                        "message": "Nothing to archive."})

    try:
        service  = _gmail_service()
        archived = 0
        failed   = 0

        for msg_id in ids:
            try:
                service.users().messages().modify(
                    userId="me",
                    id=msg_id,
                    body={"removeLabelIds": ["INBOX"]},
                ).execute()
                archived += 1
            except Exception as exc:
                app.logger.warning("Could not archive %s: %s", msg_id, exc)
                failed += 1

        # Prevent double-archive
        meta["email_ids"] = []
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        result: dict = {"success": True, "archived": archived}
        if failed:
            result["warnings"] = f"{failed} email(s) could not be archived."
        return jsonify(result)

    except Exception as exc:
        app.logger.error("Archive error: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500


def request_is_preflight():
    return request.method == "OPTIONS"


# ── Settings page ─────────────────────────────────────────────────────────────

def _load_ignored() -> set:
    p = BASE_DIR / "ignored_domains.json"
    return set(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else set()


def _save_ignored(ignored: set) -> None:
    p = BASE_DIR / "ignored_domains.json"
    p.write_text(json.dumps(sorted(ignored), indent=2), encoding="utf-8")


def _fmt_last_seen(iso: str) -> str:
    """Turn an ISO date string into a short human label like 'Apr 18'."""
    if not iso:
        return ""
    try:
        from datetime import datetime as _dt
        d = _dt.fromisoformat(iso[:10])
        return d.strftime("%-d %b")
    except Exception:
        return ""


def _build_settings_html(domains: list, ignored: set) -> str:
    rows = ""
    for d in domains:
        domain    = d["domain"]
        brand     = d["brand"]
        count     = d["count"]
        last_seen = _fmt_last_seen(d.get("last_seen", ""))
        checked   = "" if domain in ignored else "checked"
        # Escape for HTML context (attributes + text)
        domain_h = html.escape(domain, quote=True)
        brand_h  = html.escape(brand,  quote=True)
        # JSON-encode for the JS string context — handles quotes, backslashes, unicode
        domain_js = json.dumps(domain)
        favicon = (
            f'<img src="https://www.google.com/s2/favicons?domain={domain_h}&sz=48" '
            f'class="fav" alt="" onerror="this.style.display=\'none\'">'
        )
        rows += f"""
        <div class="domain-row" id="row-{domain_h}">
          {favicon}
          <div class="domain-info">
            <span class="brand">{brand_h}</span>
            <span class="addr">{domain_h}</span>
          </div>
          <span class="pill">{count} email{"s" if count != 1 else ""}{f" · {last_seen}" if last_seen else ""}</span>
          <label class="toggle">
            <input type="checkbox" {checked}
                   onchange="toggleDomain({domain_js}, this.checked)">
            <span class="slider"></span>
          </label>
        </div>"""

    if not rows:
        rows = '<p class="empty">Run the digest first to populate the domain list.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Digest Settings</title>
  <style>
    *, *::before, *::after {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;
      background:#F0F4F8; color:#0F172A; min-height:100vh;
    }}
    .page-header {{
      background:linear-gradient(135deg,#1E3A8A 0%,#2563EB 60%,#3B82F6 100%);
      color:#fff; padding:36px 48px;
    }}
    .page-header h1 {{ font-size:28px; font-weight:800; margin-bottom:6px; }}
    .page-header p  {{ font-size:15px; opacity:.75; margin-bottom:16px; }}
    .back-link {{
      color:rgba(255,255,255,.8); font-size:14px; font-weight:600;
      text-decoration:none;
      background:rgba(255,255,255,.15); padding:7px 18px;
      border-radius:20px; border:1px solid rgba(255,255,255,.25);
      display:inline-block; transition:background .2s;
    }}
    .back-link:hover {{ background:rgba(255,255,255,.25); color:#fff; }}

    .container {{ max-width:800px; margin:36px auto; padding:0 32px; }}

    .section-title {{
      font-size:16px; font-weight:700; color:#475569;
      text-transform:uppercase; letter-spacing:.8px;
      margin-bottom:16px;
    }}

    .domain-list {{
      background:#fff; border-radius:16px;
      box-shadow:0 1px 4px rgba(0,0,0,.07); overflow:hidden;
    }}

    .domain-row {{
      display:flex; align-items:center; gap:16px;
      padding:18px 24px; border-bottom:1px solid #F1F5F9;
      transition:background .15s;
    }}
    .domain-row:last-child {{ border-bottom:none; }}
    .domain-row:hover {{ background:#FAFBFF; }}

    .fav {{
      width:36px; height:36px; border-radius:8px;
      object-fit:contain; background:#F8FAFC; padding:3px; flex-shrink:0;
    }}

    .domain-info {{ flex:1; min-width:0; }}
    .brand {{
      display:block; font-size:17px; font-weight:700;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    }}
    .addr {{
      display:block; font-size:13px; color:#94A3B8; margin-top:2px;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    }}

    .pill {{
      font-size:12px; color:#64748B; background:#F1F5F9;
      padding:4px 12px; border-radius:14px; font-weight:600;
      white-space:nowrap; flex-shrink:0;
    }}

    /* Toggle switch */
    .toggle {{ position:relative; width:50px; height:28px; flex-shrink:0; }}
    .toggle input {{ opacity:0; width:0; height:0; }}
    .slider {{
      position:absolute; cursor:pointer;
      top:0; left:0; right:0; bottom:0;
      background:#CBD5E1; border-radius:34px;
      transition:.3s;
    }}
    .slider::before {{
      content:""; position:absolute;
      height:22px; width:22px; left:3px; bottom:3px;
      background:#fff; border-radius:50%; transition:.3s;
      box-shadow:0 1px 4px rgba(0,0,0,.2);
    }}
    .toggle input:checked + .slider {{ background:#2563EB; }}
    .toggle input:checked + .slider::before {{ transform:translateX(22px); }}

    .status-msg {{
      position:fixed; bottom:28px; left:50%; transform:translateX(-50%);
      background:#0F172A; color:#fff; font-size:14px; font-weight:600;
      padding:12px 24px; border-radius:12px; opacity:0;
      transition:opacity .3s; pointer-events:none; z-index:100;
      box-shadow:0 4px 20px rgba(0,0,0,.2);
    }}
    .status-msg.show {{ opacity:1; }}

    .empty {{ padding:32px; text-align:center; color:#94A3B8; font-size:15px; }}

    /* ── Mode toggle button ── */
    .mode-toggle {{
      position:absolute; right:48px; top:36px;
      background:rgba(255,255,255,.15); border:1px solid rgba(255,255,255,.25);
      color:#fff; width:38px; height:38px; border-radius:50%;
      font-size:18px; cursor:pointer; transition:background .2s;
      display:flex; align-items:center; justify-content:center;
    }}
    .mode-toggle:hover {{ background:rgba(255,255,255,.3); }}

    /* ── Dark mode overrides ── */
    body.dark {{ background:#0B1220; color:#E2E8F0; }}
    body.dark .page-header {{
      background:linear-gradient(135deg,#0B1220 0%,#1E293B 60%,#334155 100%);
    }}
    body.dark .domain-list {{ background:#1E293B; }}
    body.dark .domain-row  {{ border-bottom-color:#334155; }}
    body.dark .domain-row:hover {{ background:#273449; }}
    body.dark .brand {{ color:#F1F5F9; }}
    body.dark .addr  {{ color:#94A3B8; }}
    body.dark .pill  {{ background:#334155; color:#CBD5E1; }}
    body.dark .section-title {{ color:#94A3B8; }}
    body.dark .empty {{ color:#64748B; }}

    @media(max-width:600px) {{
      .page-header {{ padding:28px 20px; }}
      .container   {{ padding:0 16px; margin:24px auto; }}
      .domain-row  {{ padding:14px 16px; gap:12px; }}
      .mode-toggle {{ right:20px; top:28px; }}
    }}
  </style>
</head>
<body>
  <div class="page-header">
    <button class="mode-toggle" id="modeToggle" onclick="toggleDark()" title="Toggle dark mode">🌙</button>
    <h1>⚙️ Digest Settings</h1>
    <p>This is your master list of every sender ever seen.
       <strong>ON</strong> = included in the digest. <strong>OFF</strong> = ignored forever.
       Changes take effect on the next run.</p>
    <a href="/" class="back-link">← Back to Digest</a>
  </div>

  <div class="container">
    <div class="section-title">All sender domains — {len(domains)} total</div>
    <div class="domain-list">
      {rows}
    </div>
  </div>

  <div class="status-msg" id="statusMsg"></div>

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

    async function toggleDomain(domain, include) {{
      try {{
        const res = await fetch('/settings/toggle', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{ domain, ignore: !include }})
        }});
        const data = await res.json();
        if (data.success) {{
          flash(include ? domain + ' will be included' : domain + ' will be ignored');
        }} else {{
          flash('Error: ' + (data.error || 'unknown'));
        }}
      }} catch(e) {{
        flash('Network error: ' + e.message);
      }}
    }}

    function flash(msg) {{
      const el = document.getElementById('statusMsg');
      el.textContent = msg;
      el.classList.add('show');
      setTimeout(() => el.classList.remove('show'), 2800);
    }}
  </script>
</body>
</html>"""


@app.route("/status", methods=["GET"])
@require_auth
def run_status():
    log_path = OUTPUT_DIR / "run_log.json"
    if not log_path.exists():
        return jsonify({"status": "unknown"})
    return jsonify(json.loads(log_path.read_text(encoding="utf-8")))


@app.route("/settings", methods=["GET"])
@require_auth
def settings():
    # Prefer cumulative all-domain history; fall back to latest run if missing.
    history_path = BASE_DIR / "all_domains.json"
    domains: list = []
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
            domains = [
                {"domain":    d["domain"],
                 "brand":     d["brand"],
                 "count":     d.get("total_count", 0),
                 "last_seen": d.get("last_seen", "")}
                for d in history
            ]
        except json.JSONDecodeError:
            domains = []

    if not domains:
        meta_path = OUTPUT_DIR / "digest_meta.json"
        if meta_path.exists():
            meta    = json.loads(meta_path.read_text(encoding="utf-8"))
            domains = meta.get("domains", [])

    ignored = _load_ignored()
    # Also show any ignored domains not present in history
    extra = [
        {"domain": d, "brand": d, "count": 0}
        for d in sorted(ignored)
        if not any(x["domain"] == d for x in domains)
    ]
    return _build_settings_html(domains + extra, ignored)


@app.route("/settings/toggle", methods=["POST", "OPTIONS"])
@require_auth
def toggle_domain():
    if request_is_preflight():
        return jsonify({}), 204

    data   = request.get_json(silent=True) or {}
    domain = data.get("domain", "").strip().lower()
    ignore = bool(data.get("ignore", False))

    if not domain:
        return jsonify({"success": False, "error": "No domain provided."}), 400

    ignored = _load_ignored()
    if ignore:
        ignored.add(domain)
    else:
        ignored.discard(domain)
    _save_ignored(ignored)

    return jsonify({"success": True, "ignored": sorted(ignored)})


# ── Local dev entry point ─────────────────────────────────────────────────────
if __name__ == "__main__":
    OUTPUT_DIR.mkdir(exist_ok=True)
    app.run(debug=True, port=8080)
