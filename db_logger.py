"""
Optional Postgres trade logging, in addition to the local paper_trades.jsonl
file. When DATABASE_URL is set (e.g. running as a Render web service linked
to a Render Postgres instance), every open/close event also gets inserted
into a `trades` table -- which lets a remote query (e.g. via the Render MCP
connector's query_render_postgres) pull real results without needing
filesystem access to wherever the bot happens to be running.

If DATABASE_URL isn't set, every function here is a silent no-op -- local
file logging (execution.py's PaperBroker._log) is unaffected either way.
"""

_table_ready = False


def _get_connection():
    """Returns a psycopg2 connection if DATABASE_URL is set, else None (file-only logging)."""
    import os
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    import psycopg2
    # Explicit sslmode=require rather than trusting psycopg2's default
    # ("prefer"). Render's Postgres enforces SSL, and we've directly
    # observed a related tool fail with "SSL/TLS required" -- forcing it
    # here removes any ambiguity about whether the bot's own writes would
    # hit the same wall, rather than assuming the default behaves correctly.
    return psycopg2.connect(url, sslmode="require")


def ensure_table():
    global _table_ready
    if _table_ready:
        return
    conn = _get_connection()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
                    action TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    strategy TEXT,
                    market_title TEXT,
                    direction TEXT,
                    quoted_price_cents DOUBLE PRECISION,
                    fill_price_cents DOUBLE PRECISION,
                    was_maker BOOLEAN,
                    size_dollars DOUBLE PRECISION,
                    fee_dollars DOUBLE PRECISION,
                    pnl_dollars DOUBLE PRECISION,
                    balance_after DOUBLE PRECISION,
                    reason TEXT
                )
            """)
            # idempotent migration for tables created before market_title existed --
            # ADD COLUMN IF NOT EXISTS never touches existing rows
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS market_title TEXT")
        _table_ready = True
    finally:
        conn.close()


def log_trade(record: dict):
    """
    Best-effort insert into Render Postgres -- NEVER raises. DB logging is a
    nice-to-have for remote querying; it must not be able to break the
    trading loop if the DB is briefly unreachable, over its free-tier
    connection limit, or unconfigured entirely (DATABASE_URL unset).
    """
    conn = _get_connection()
    if conn is None:
        return
    try:
        ensure_table()
        with conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trades (action, ticker, strategy, market_title, direction, quoted_price_cents,
                    fill_price_cents, was_maker, size_dollars, fee_dollars, pnl_dollars,
                    balance_after, reason)
                VALUES (%(action)s, %(ticker)s, %(strategy)s, %(market_title)s, %(direction)s, %(quoted_price_cents)s,
                    %(fill_price_cents)s, %(was_maker)s, %(size_dollars)s, %(fee_dollars)s,
                    %(pnl_dollars)s, %(balance_after)s, %(reason)s)
            """, {
                "action": record.get("action"),
                "ticker": record.get("ticker"),
                "strategy": record.get("strategy"),
                "market_title": record.get("market_title"),
                "direction": record.get("direction"),
                "quoted_price_cents": record.get("quoted_price_cents") or record.get("quoted_exit_price_cents"),
                "fill_price_cents": record.get("fill_price_cents"),
                "was_maker": record.get("was_maker"),
                "size_dollars": record.get("size_dollars"),
                "fee_dollars": record.get("fee_dollars"),
                "pnl_dollars": record.get("pnl_dollars"),
                "balance_after": record.get("balance_after"),
                "reason": record.get("reason"),
            })
    except Exception as e:
        print(f"[warn] db logging failed (continuing without it): {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
