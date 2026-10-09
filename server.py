#!/usr/bin/env python3
"""Trigger API: a tiny local test endpoint for the compliance case.

POST /triggers   report a trigger {url, title, description?}
GET  /triggers   list all triggers (JSON for clients, an HTML page for browsers)

Python standard library only. Configuration through environment variables:
PORT (8080), HOST (0.0.0.0), MAX_TRIGGERS (1000), DATA_FILE (unset = in memory only).
"""
import html
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_TRIGGERS = int(os.environ.get("MAX_TRIGGERS", "1000"))
DATA_FILE = os.environ.get("DATA_FILE") or None
MAX_BODY = 64 * 1024
LIMITS = {"url": 2000, "title": 200, "description": 2000}

EXAMPLE = {"url": "https://example.com/calls/42?t=95", "title": "Possible insider trading",
           "description": "Customer wants to buy before the announcement."}


class Store:
    """Triggers in memory, optionally mirrored to a JSON file."""

    def __init__(self, path=None, limit=MAX_TRIGGERS):
        self.path, self.limit = (Path(path) if path else None), limit
        self.lock = threading.Lock()
        self.items, self.next_id = [], 1
        if self.path and self.path.is_file():
            self.items = json.loads(self.path.read_text() or "[]")
            self.next_id = max((t["id"] for t in self.items), default=0) + 1

    def _save(self):
        if self.path:
            self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=2))

    def add(self, url, title, description):
        with self.lock:
            item = {"id": self.next_id, "url": url, "title": title, "description": description,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self.next_id += 1
            self.items.append(item)
            del self.items[:-self.limit]   # keep only the newest `limit` triggers
            self._save()
            return item

    def all(self):
        with self.lock:
            return list(self.items)

    def clear(self):
        with self.lock:
            self.items = []
            self._save()


def validate(data):
    """Returns (clean fields, None) or (None, error message)."""
    if not isinstance(data, dict):
        return None, "body must be a JSON object"
    title = data.get("title", data.get("titel"))   # "titel" accepted as an alias
    fields = {"url": data.get("url"), "title": title, "description": data.get("description")}
    for name in ("url", "title"):
        if not isinstance(fields[name], str) or not fields[name].strip():
            return None, f"'{name}' is required and must be a non-empty string"
    if fields["description"] is None:
        fields["description"] = ""
    if not isinstance(fields["description"], str):
        return None, "'description' must be a string"
    fields = {k: v.strip() for k, v in fields.items()}
    for name, limit in LIMITS.items():
        if len(fields[name]) > limit:
            return None, f"'{name}' is longer than {limit} characters"
    parsed = urlparse(fields["url"])
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, "'url' must be an absolute http or https URL"
    return fields, None


def page(triggers):
    rows = "".join(
        f"""<li><a href="{html.escape(t['url'], quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(t['title'])}</a>
        <span class="meta">#{t['id']} · {html.escape(t['created_at'])}</span>
        {f'<p>{html.escape(t["description"])}</p>' if t['description'] else ''}
        <code>{html.escape(t['url'])}</code></li>"""
        for t in reversed(triggers))
    empty = '<p class="empty">No triggers yet. Send one with POST /triggers, see <a href="/docs">the docs</a>.</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5"><title>Trigger API</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; margin: 0; background: #f7f6f2; color: #121417; }}
main {{ max-width: 860px; margin: 0 auto; padding: 24px 16px 48px; }}
header {{ display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }}
h1 {{ font-size: 24px; margin: 0; }} header span, .meta, .empty {{ color: #6e6e76; }}
header a {{ margin-left: auto; }}
ul {{ list-style: none; padding: 0; margin: 24px 0 0; }}
li {{ background: #fff; border: 1px solid #e4e0d6; border-radius: 10px; padding: 14px 16px; margin-bottom: 10px; }}
li > a {{ font-size: 18px; font-weight: 600; color: #0268c8; }}
.meta {{ font-size: 13px; margin-left: 8px; }} p {{ margin: 6px 0; }}
code {{ font-size: 13px; color: #6e6e76; word-break: break-all; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #121417; color: #f3f1ec; }} li {{ background: #1a1d21; border-color: #2e3138; }}
  li > a {{ color: #7ab8ff; }} header span, .meta, .empty, code {{ color: #9a98a0; }} a {{ color: #7ab8ff; }}
}}
</style></head><body><main>
<header><h1>Trigger API</h1><span>{len(triggers)} trigger{'' if len(triggers) == 1 else 's'} · refreshes every 5 s</span><a href="/docs">Docs</a></header>
{f'<ul>{rows}</ul>' if triggers else empty}
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    store = None
    server_version = "TriggerAPI/1.0"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def send(self, code, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        # Open CORS: browser front ends on any origin may call this test API.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if code != 204:
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def wants_html(self, query):
        fmt = parse_qs(query).get("format", [""])[0]
        if fmt in ("json", "html"):
            return fmt == "html"
        return "text/html" in (self.headers.get("Accept") or "")

    def do_OPTIONS(self):
        self.send(204)

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        if path == "/":
            return self.send(200, page(self.store.all()), "text/html")
        if path == "/triggers":
            triggers = self.store.all()
            if self.wants_html(url.query):
                return self.send(200, page(triggers), "text/html")
            return self.send(200, {"count": len(triggers), "triggers": triggers})
        if path == "/docs":
            return self.send(200, (HERE / "docs.html").read_bytes(), "text/html")
        if path == "/health":
            return self.send(200, {"status": "ok"})
        self.send(404, {"error": "not found", "endpoints": ["POST /triggers", "GET /triggers", "GET /docs"]})

    do_HEAD = do_GET

    def do_POST(self):
        if (urlparse(self.path).path.rstrip("/") or "/") != "/triggers":
            return self.send(404, {"error": "not found", "endpoints": ["POST /triggers", "GET /triggers"]})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            return self.send(413, {"error": f"body larger than {MAX_BODY} bytes"})
        try:
            data = json.loads(self.rfile.read(length) or b"")
        except (ValueError, UnicodeDecodeError):
            return self.send(400, {"error": "body must be valid JSON", "example": EXAMPLE})
        fields, error = validate(data)
        if error:
            return self.send(400, {"error": error, "example": EXAMPLE})
        self.send(201, self.store.add(**fields))

    def do_DELETE(self):
        if (urlparse(self.path).path.rstrip("/") or "/") != "/triggers":
            return self.send(404, {"error": "not found"})
        self.store.clear()
        self.send(204)


def make_server(host=HOST, port=PORT, data_file=DATA_FILE, limit=MAX_TRIGGERS):
    handler = type("BoundHandler", (Handler,), {"store": Store(data_file, limit)})
    return ThreadingHTTPServer((host, port), handler)


if __name__ == "__main__":
    server = make_server()
    print(f"Trigger API listening on http://{HOST}:{PORT}  (list: /triggers, docs: /docs)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
