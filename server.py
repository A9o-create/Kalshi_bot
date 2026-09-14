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


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass  # keep Render's log output focused on the bot, not health-check noise


def main():
    bot_thread = threading.Thread(target=live_paper_runner.run, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 10000))
    print(f"Health endpoint listening on :{port}, bot running in background thread")
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


if __name__ == "__main__":
    main()
