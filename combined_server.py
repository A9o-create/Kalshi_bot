"""
Render deployment entrypoint for the combined bot (tennis + momentum in one
process, sharing one broker -- see combined_runner.py). Same wrapping
pattern as the original server.py/momentum_server.py, but the health check
now needs to verify BOTH loop threads are alive, not just one -- a thread
that dies silently (like the sys.exit() bugs found earlier this session)
would otherwise leave the health check reporting "live" while half the bot
had actually stopped.

Start command on Render: python combined_server.py
"""

import os
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import combined_runner
import db_logger

run_thread = None  # the thread running combined_runner.run(), which itself
                    # spawns the tennis_loop/momentum_loop threads


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            self._handle_status()
        elif self.path.startswith("/trades"):
            self._handle_trades()
        else:
            self._handle_health()

    def _handle_health(self):
        """
        Alive only if BOTH the outer run() thread AND both inner loop
        threads are still running. threading.enumerate() is used to find
        the inner threads by name since combined_runner doesn't expose
        them as module-level references (they're local to run()).
        """
        outer_alive = run_thread is not None and run_thread.is_alive()
        thread_names = {t.name for t in threading.enumerate() if t.is_alive()}
        tennis_alive = "tennis_loop" in thread_names
        momentum_alive = "momentum_loop" in thread_names
        alive = outer_alive and tennis_alive and momentum_alive

        self.send_response(200 if alive else 503)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if alive:
            self.wfile.write(b"ok")
        else:
            detail = f"outer={outer_alive} tennis={tennis_alive} momentum={momentum_alive}"
            self.wfile.write(detail.encode())

    def _handle_status(self):
        """Live in-memory combined state -- one shared balance/position count, no DB needed."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(combined_runner.latest_status).encode())

    def _handle_trades(self):
        """
        Unfiltered now -- both legs share one balance/ledger, so there's no
        longer a reason to split the view by strategy the way the two
        separate bots' endpoints did. Same DB connection path as before
        (app's own psycopg2, sslmode=require) bypassing Render's broken
        query tool.
        """
        try:
            conn = db_logger._get_connection()
            if conn is None:
                raise RuntimeError("DATABASE_URL not set")
            with conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT 50")
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
    global run_thread
    run_thread = threading.Thread(target=combined_runner.run, daemon=True, name="combined_run")
    run_thread.start()

    port = int(os.environ.get("PORT", 10000))
    print(f"[combined bot] Health endpoint listening on :{port}, both loops running in background threads")
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


if __name__ == "__main__":
    main()
