#!/usr/bin/env python3
"""
Digest Server — Phases 3 & 4.

  GET  /          Serves output/digest.html in your browser
  POST /archive   Removes INBOX label from every email in the digest (Phase 4)

Run:  python serve.py
Stop: Ctrl+C
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Google ────────────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

OUTPUT_DIR = Path("output")
PORT       = 8080
SCOPES     = ["https://www.googleapis.com/auth/gmail.modify"]


def _gmail_service():
    token_path = Path("token.json")
    if not token_path.exists():
        raise RuntimeError("token.json not found. Run digest.py first to authenticate.")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


# ─────────────────────────────────────────────────────────────────────────────
# Request handler
# ─────────────────────────────────────────────────────────────────────────────

class DigestHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  [{self.address_string()}] {fmt % args}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    # ── GET / and /settings ───────────────────────────────────────────────────
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            digest = OUTPUT_DIR / "digest.html"
            if not digest.exists():
                return self._text(
                    404, "Digest not found.\nRun `python digest.py` first."
                )
            self._send_html(digest.read_bytes())
        elif self.path == "/settings":
            self._handle_settings_page()
        elif self.path == "/status":
            log_path = OUTPUT_DIR / "run_log.json"
            data = json.loads(log_path.read_text(encoding="utf-8")) \
                   if log_path.exists() else {"status": "unknown"}
            self._json(200, data)
        else:
            self._text(404, "Not found.")

    def _handle_settings_page(self):
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from flask_app import _build_settings_html, _load_ignored
        meta_path = OUTPUT_DIR / "digest_meta.json"
        domains   = []
        if meta_path.exists():
            meta    = json.loads(meta_path.read_text(encoding="utf-8"))
            domains = meta.get("domains", [])
        ignored = _load_ignored()
        extra   = [
            {"domain": d, "brand": d, "count": 0}
            for d in sorted(ignored)
            if not any(x["domain"] == d for x in domains)
        ]
        html = _build_settings_html(domains + extra, ignored).encode("utf-8")
        self._send_html(html)

    def _send_html(self, content: bytes):
        self.send_response(200)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    # ── POST /archive  /settings/toggle ──────────────────────────────────────
    def do_POST(self):
        if self.path == "/archive":
            self._handle_archive()
        elif self.path == "/settings/toggle":
            self._handle_toggle()
        else:
            self._json(404, {"success": False, "error": "Not found."})

    def _handle_toggle(self):
        length  = int(self.headers.get("Content-Length", 0))
        body    = self.rfile.read(length)
        data    = json.loads(body) if body else {}
        domain  = data.get("domain", "").strip().lower()
        ignore  = bool(data.get("ignore", False))
        if not domain:
            return self._json(400, {"success": False, "error": "No domain provided."})
        ignored_path = Path("ignored_domains.json")
        ignored      = set(json.loads(ignored_path.read_text(encoding="utf-8"))
                           if ignored_path.exists() else [])
        if ignore:
            ignored.add(domain)
        else:
            ignored.discard(domain)
        ignored_path.write_text(json.dumps(sorted(ignored), indent=2), encoding="utf-8")
        self._json(200, {"success": True, "ignored": sorted(ignored)})

    def _handle_archive(self):
        meta_path = OUTPUT_DIR / "digest_meta.json"

        if not meta_path.exists():
            return self._json(
                400,
                {
                    "success": False,
                    "error":   "No digest metadata found. Run digest.py first.",
                },
            )

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        ids  = meta.get("email_ids", [])

        if not ids:
            return self._json(
                200, {"success": True, "archived": 0, "message": "Nothing to archive."}
            )

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
                    print(f"  ⚠  Could not archive {msg_id}: {exc}")
                    failed += 1

            # Clear IDs to prevent accidental double-archive
            meta["email_ids"] = []
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

            result: dict = {"success": True, "archived": archived}
            if failed:
                result["warnings"] = f"{failed} email(s) could not be archived."
            self._json(200, result)

        except Exception as exc:
            self._json(500, {"success": False, "error": str(exc)})

    # ── Low-level response helpers ────────────────────────────────────────────
    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, msg: str):
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type",   "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    OUTPUT_DIR.mkdir(exist_ok=True)
    server = HTTPServer(("localhost", PORT), DigestHandler)

    print(f"\n🌐 Digest server → http://localhost:{PORT}")
    print("   POST /archive   to move all inbox emails to archive")
    print("   Ctrl+C          to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Server stopped.")
        sys.exit(0)
