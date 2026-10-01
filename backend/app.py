import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend" / "index.html"
MONITOR = ROOT / "frontend" / "monitor.html"

lock = Lock()
stats = {
    "requests": 0,
    "received_bytes": 0,
    "sent_bytes": 0,
    "clients": set(),
    "events": deque(maxlen=100),
}

def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def event(direction, client, method, size, text):
    with lock:
        stats["events"].appendleft({
            "time": now_utc(),
            "direction": direction,
            "client": client,
            "method": method,
            "bytes": size,
            "text": text,
        })

class Handler(BaseHTTPRequestHandler):
    def send_body(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if self.path.startswith("/monitor"):
            return
        super().log_message(format, *args)

    def do_GET(self):
        if self.path == "/":
            body = FRONTEND.read_bytes()
            client = self.client_address[0]
            with lock:
                stats["requests"] += 1
                stats["sent_bytes"] += len(body)
                stats["clients"].add(client)
            event("OUT", client, "GET", len(body), "index.html")
            self.send_body(200, body, "text/html; charset=utf-8")
            return

        if self.path == "/monitor":
            body = MONITOR.read_bytes()
            self.send_body(200, body, "text/html; charset=utf-8")
            return

        if self.path == "/monitor-data":
            with lock:
                snapshot = {
                    "requests": stats["requests"],
                    "received_bytes": stats["received_bytes"],
                    "sent_bytes": stats["sent_bytes"],
                    "clients": len(stats["clients"]),
                    "events": list(stats["events"]),
                }
            body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
            self.send_body(200, body, "application/json; charset=utf-8")
            return

        self.send_error(404)

    def do_POST(self):
        if self.path != "/":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        client = self.client_address[0]
        text = body.decode("utf-8", errors="replace")

        with lock:
            stats["requests"] += 1
            stats["received_bytes"] += len(body)
            stats["sent_bytes"] += len(body)
            stats["clients"].add(client)

        event("IN", client, "POST", len(body), text)
        event("OUT", client, "POST", len(body), text)
        self.send_body(200, body, "text/plain; charset=utf-8")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"listening on port {port}")
    server.serve_forever()
