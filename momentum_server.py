"""
Render deployment entrypoint for the standalone momentum (BTC) bot -- same
pattern as server.py (the tennis bot's entrypoint), wrapping
momentum_runner.run() instead. Deployed as its own separate Render service
so redeploying BTC-specific changes never touches the tennis bot's running
state, and vice versa.

Start command on Render: python momentum_server.py
"""

import os
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import momentum_runner
import db_logger

bot_thread = None  # module-level so the health handler can check its liveness


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            self._handle_status()
        elif self.path.startswith("/trades"):
            self._handle_trades()
        else:
            self._handle_health()

    def _handle_health(self):
        alive = bot_thread is not None and bot_thread.is_alive()
        self.send_response(200 if alive else 503)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok" if alive else b"bot thread is not running")

    def _handle_status(self):
        """Live in-memory bot state -- this bot's own balance/positions, no DB involved."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(momentum_runner.latest_status).encode())

    def _handle_trades(self):
        """
        Same shared trades table as the tennis bot (both bots write to the
        same Postgres, distinguished by the strategy column) -- filtered to
        strategy='momentum' here so this endpoint shows only THIS bot's
        trades, not a mixed view of both bots.
        """
        try:
            conn = db_logger._get_connection()
            if conn is None:
                raise RuntimeError("DATABASE_URL not set")
            with conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM trades WHERE strategy = 'momentum' ORDER BY ts DESC LIMIT 50")
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            conn.close()
            body = json.dumps(rows, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass  # keep Render's log output focused on the bot, not health-check noise


def main():
    global bot_thread
    bot_thread = threading.Thread(target=momentum_runner.run, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 10000))
    print(f"[momentum bot] Health endpoint listening on :{port}, bot running in background thread")
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


if __name__ == "__main__":
    main()
