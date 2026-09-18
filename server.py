"""
Render deployment entrypoint for the tennis bot (mean reversion + value
entry). A Render web service must bind to a port and respond to health
checks -- but the actual bot is a long-running loop with no HTTP interface
of its own. This wraps live_paper_runner.run() (completely unchanged --
same continuous while-loop as running it locally in tmux) in a background
thread, and serves a trivial /health endpoint on the side so Render
considers the service healthy.

Deployed as a separate Render service from the momentum (BTC) bot -- see
momentum_server.py / momentum_runner.py -- so redeploying either one never
resets the other's accumulated in-memory balance/positions.

This is NOT a cron job on purpose: a cron job would restart as a fresh
process on every invocation, wiping PaperBroker's in-memory balance and open
positions each time. This needs to be one continuous process, exactly like
running it in tmux -- Render just needs a reason to keep it alive.

Start command on Render: python server.py
"""

import os
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import live_paper_runner
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
        # Reflect whether the bot thread is actually still running, not just
        # whether this process is up -- a thread that exits silently (e.g.
        # via an uncaught sys.exit() inside it) doesn't kill the process, so
        # a naive always-200 health check would keep reporting "live" even
        # after the bot stopped doing anything. That's exactly what happened
        # here: config.ENVIRONMENT wasn't reading the env var, the trading
        # loop exited immediately, and the health check never caught it.
        alive = bot_thread is not None and bot_thread.is_alive()
        self.send_response(200 if alive else 503)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok" if alive else b"bot thread is not running")

    def _handle_status(self):
        """
        Live in-memory bot state, no database involved at all -- this is the
        most reliable way to check on the bot, since it can't be broken by
        any database connectivity issue on either end.
        """
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(live_paper_runner.latest_status).encode())

    def _handle_trades(self):
        """
        Same shared trades table as the momentum bot (both bots write to the
        same Postgres, distinguished by the strategy column) -- filtered to
        the two tennis strategies here so this endpoint shows only THIS
        bot's trades, not a mixed view of both bots. Queries the app's own
        psycopg2 connection (sslmode=require, the same path that's been used
        for every successful write) -- bypassing Render's query tool, which
        has an unrelated SSL negotiation bug on its own connection path.
        """
        try:
            conn = db_logger._get_connection()
            if conn is None:
                raise RuntimeError("DATABASE_URL not set")
            with conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM trades WHERE strategy IN ('reversion', 'value_entry') ORDER BY ts DESC LIMIT 50")
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
    bot_thread = threading.Thread(target=live_paper_runner.run, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 10000))
    print(f"Health endpoint listening on :{port}, bot running in background thread")
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


if __name__ == "__main__":
    main()
