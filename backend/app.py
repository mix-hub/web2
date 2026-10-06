import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mysql.connector
from argon2 import PasswordHasher

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend" / "index.html"
MONITOR = ROOT / "frontend" / "monitor.html"

lock = Lock()
password_hasher = PasswordHasher()

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


def db_connect():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "web2"),
    )


def validate_login_id(login_id):
    return isinstance(login_id, str) and bool(login_id.strip())


def validate_password(password):
    return isinstance(password, str) and bool(password)


class Handler(BaseHTTPRequestHandler):
    def send_body(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_body(status, body, "application/json; charset=utf-8")

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
        if self.path == "/signup":
            self.handle_signup()
            return

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

    def handle_signup(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        client = self.client_address[0]

        with lock:
            stats["requests"] += 1
            stats["received_bytes"] += len(body)
            stats["clients"].add(client)

        event("IN", client, "POST", len(body), "signup")

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "invalid JSON"})
            return

        login_id = data.get("login_id")
        password = data.get("password")

        if not validate_login_id(login_id):
            self.send_json(400, {"error": "login_id is required"})
            return

        if not validate_password(password):
            self.send_json(400, {"error": "password is required"})
            return

        login_id = login_id.strip()

        conn = None
        cursor = None

        try:
            conn = db_connect()
            cursor = conn.cursor()

            cursor.execute(
                "SELECT user_id FROM users WHERE login_id = %s",
                (login_id,),
            )
            if cursor.fetchone() is not None:
                self.send_json(409, {"error": "login_id already exists"})
                return

            pw_hash = password_hasher.hash(password)

            cursor.execute(
                """
                INSERT INTO users (login_id, pw_hash, created_at)
                VALUES (%s, %s, UTC_TIMESTAMP())
                """,
                (login_id, pw_hash),
            )
            conn.commit()

            response = {
                "ok": True,
                "user_id": cursor.lastrowid,
            }
            response_body = json.dumps(response, ensure_ascii=False).encode("utf-8")

            with lock:
                stats["sent_bytes"] += len(response_body)

            event("OUT", client, "POST", len(response_body), "signup success")
            self.send_body(201, response_body, "application/json; charset=utf-8")

        except mysql.connector.Error as error:
            if conn is not None:
                conn.rollback()
            print(f"database error: {error}")
            self.send_json(500, {"error": "database error"})

        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None and conn.is_connected():
                conn.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"listening on port {port}")
    server.serve_forever()
