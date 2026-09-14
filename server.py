"""
Render deployment entrypoint. A Render web service must bind to a port and
respond to health checks -- but the actual bot is a long-running loop with
no HTTP interface of its own. This wraps live_paper_runner.run() (completely
unchanged -- same continuous while-loop as running it locally in tmux) in a
background thread, and serves a trivial /health endpoint on the side so
Render considers the service healthy.

This is NOT a cron job on purpose: a cron job would restart as a fresh
process on every invocation, wiping PaperBroker's in-memory balance and open
positions each time. This needs to be one continuous process, exactly like
running it in tmux -- Render just needs a reason to keep it alive.

Start command on Render: python server.py
"""

import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import live_paper_runner

bot_thread = None  # module-level so the health handler can check its liveness


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
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
