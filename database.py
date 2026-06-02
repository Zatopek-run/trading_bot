"""
SQLite persistence (async via aiosqlite).
Stores executed orders, derived trades (paired BUY/SELL), equity snapshots,
and experiment metadata. Feeds the dashboard.
"""
from __future__ import annotations
import time
import logging
import aiosqlite
from datetime import datetime, timezone
from config import (DB_PATH, INITIAL_CAPITAL, GOAL_CAPITAL, EXPERIMENT_DAYS,
                    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_STOP_PCT)

log = logging.getLogger(__name__)


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            symbol      TEXT    NOT NULL,
            side        TEXT    NOT NULL,         -- BUY / SELL
            qty         REAL    NOT NULL,
            price       REAL    NOT NULL,
            order_id    TEXT,
            score       INTEGER
        );

        CREATE TABLE IF NOT EXISTS trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol        TEXT    NOT NULL,
            direction     TEXT    NOT NULL,       -- LONG / SHORT
            qty           REAL    NOT NULL,
            entry_price   REAL    NOT NULL,
            exit_price    REAL,
            entry_ts      REAL    NOT NULL,
            exit_ts       REAL,
            status        TEXT    NOT NULL,       -- open / closed
            pnl           REAL    DEFAULT 0,
            pnl_pct       REAL    DEFAULT 0,
            sl_price      REAL,
            tp_price      REAL,
            peak_price    REAL,
            close_reason  TEXT
        );

        CREATE TABLE IF NOT EXISTS equity (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              REAL    NOT NULL,
            portfolio_value REAL    NOT NULL,
            btc_buyhold     REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        await db.commit()

        # Migration: add SL/TP columns to pre-existing trades tables.
        async with db.execute("PRAGMA table_info(trades)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        for col, ddl in (("sl_price", "REAL"), ("tp_price", "REAL"),
                         ("peak_price", "REAL"), ("close_reason", "TEXT")):
            if col not in cols:
                await db.execute(f"ALTER TABLE trades ADD COLUMN {col} {ddl}")
        await db.commit()

        # Seed experiment metadata once.
        async with db.execute("SELECT value FROM meta WHERE key='start_ts'") as cur:
            row = await cur.fetchone()
        if row is None:
            now = time.time()
            await _set_meta(db, "start_ts",        str(now))
            await _set_meta(db, "initial_capital", str(INITIAL_CAPITAL))
            await _set_meta(db, "goal_capital",    str(GOAL_CAPITAL))
            await _set_meta(db, "experiment_days", str(EXPERIMENT_DAYS))
            await db.commit()
    log.info("Database ready at %s", DB_PATH)


async def _set_meta(db, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


async def get_meta(key: str, default: str | None = None) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM meta WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else default


async def set_meta(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await _set_meta(db, key, value)
        await db.commit()


def _sl_tp(direction: str, entry_price: float) -> tuple[float, float]:
    """Returns (sl_price, tp_price) for a freshly opened position."""
    if direction == "LONG":
        return (entry_price * (1 - STOP_LOSS_PCT / 100),
                entry_price * (1 + TAKE_PROFIT_PCT / 100))
    return (entry_price * (1 + STOP_LOSS_PCT / 100),
            entry_price * (1 - TAKE_PROFIT_PCT / 100))


async def record_order(symbol: str, side: str, qty: float, price: float,
                       order_id: str, score: int,
                       close_reason: str = "manual") -> dict:
    """
    Records an executed order and updates the paired-trade ledger.
    BUY opens/extends a LONG; an opposite SELL closes it (and vice versa).
    On open, computes and stores SL/TP levels. Returns the trade row affected.
    """
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO orders(ts,symbol,side,qty,price,order_id,score) "
            "VALUES(?,?,?,?,?,?,?)",
            (now, symbol, side, qty, price, str(order_id), score),
        )

        # Find an open trade on this symbol.
        async with db.execute(
            "SELECT id,direction,qty,entry_price FROM trades "
            "WHERE symbol=? AND status='open' ORDER BY id DESC LIMIT 1",
            (symbol,),
        ) as cur:
            open_trade = await cur.fetchone()

        opening_dir = "LONG" if side == "BUY" else "SHORT"

        def _open(direction):
            sl, tp = _sl_tp(direction, price)
            return db.execute(
                "INSERT INTO trades(symbol,direction,qty,entry_price,entry_ts,"
                "status,sl_price,tp_price,peak_price) VALUES(?,?,?,?,?, 'open',?,?,?)",
                (symbol, direction, qty, price, now, sl, tp, price),
            )

        if open_trade is None:
            await _open(opening_dir)
            await db.commit()
            sl, tp = _sl_tp(opening_dir, price)
            result = {"action": "open", "direction": opening_dir,
                      "symbol": symbol, "sl_price": sl, "tp_price": tp}
        else:
            tid, direction, t_qty, entry_price = open_trade
            closes = (direction == "LONG" and side == "SELL") or \
                     (direction == "SHORT" and side == "BUY")
            if closes:
                if direction == "LONG":
                    pnl = (price - entry_price) * t_qty
                else:
                    pnl = (entry_price - price) * t_qty
                pnl_pct = (pnl / (entry_price * t_qty)) * 100 if entry_price else 0
                await db.execute(
                    "UPDATE trades SET exit_price=?, exit_ts=?, status='closed', "
                    "pnl=?, pnl_pct=?, close_reason=? WHERE id=?",
                    (price, now, pnl, pnl_pct, close_reason, tid),
                )
                await db.commit()
                result = {"action": "close", "direction": direction,
                          "symbol": symbol, "pnl": pnl, "pnl_pct": pnl_pct,
                          "close_reason": close_reason}
            else:
                await _open(opening_dir)
                await db.commit()
                sl, tp = _sl_tp(opening_dir, price)
                result = {"action": "open", "direction": opening_dir,
                          "symbol": symbol, "sl_price": sl, "tp_price": tp}
    return result


async def record_equity(portfolio_value: float, btc_buyhold: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO equity(ts,portfolio_value,btc_buyhold) VALUES(?,?,?)",
            (time.time(), portfolio_value, btc_buyhold),
        )
        await db.commit()


async def realized_pnl() -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='closed'"
        ) as cur:
            row = await cur.fetchone()
    return float(row[0] or 0)


async def update_trailing_sl(trade_id: int, new_sl: float, new_peak: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE trades SET sl_price=?, peak_price=? WHERE id=?",
            (new_sl, new_peak, trade_id),
        )
        await db.commit()


# ── Dashboard read queries ───────────────────────────────────────────────────

async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async def meta(k, d):
            async with db.execute("SELECT value FROM meta WHERE key=?", (k,)) as c:
                r = await c.fetchone()
            return r["value"] if r else d

        initial = float(await meta("initial_capital", INITIAL_CAPITAL))
        goal    = float(await meta("goal_capital", GOAL_CAPITAL))
        start   = float(await meta("start_ts", time.time()))
        days    = int(float(await meta("experiment_days", EXPERIMENT_DAYS)))

        async with db.execute(
            "SELECT COUNT(*) n, "
            "COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) wins, "
            "COALESCE(SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END),0) losses, "
            "COALESCE(SUM(pnl),0) total_pnl, "
            "COALESCE(MAX(pnl_pct),0) best, "
            "COALESCE(MIN(pnl_pct),0) worst "
            "FROM trades WHERE status='closed'"
        ) as cur:
            t = await cur.fetchone()

        async with db.execute(
            "SELECT portfolio_value, btc_buyhold FROM equity ORDER BY id DESC LIMIT 1"
        ) as cur:
            last_eq = await cur.fetchone()

    closed   = t["n"]
    wins     = t["wins"]
    # Usa l'ultimo snapshot equity che include anche l'unrealized PnL
    final    = float(last_eq["portfolio_value"]) if last_eq else initial + float(t["total_pnl"])
    win_rate = (wins / closed * 100) if closed else 0.0
    btc_bh   = float(last_eq["btc_buyhold"]) if last_eq else initial

    return {
        "initial_capital": initial,
        "final_capital":   final,
        "goal_capital":    goal,
        "goal_reached":    final >= goal,
        "btc_buyhold":     btc_bh,
        "btc_buyhold_pct": (btc_bh / initial - 1) * 100 if initial else 0,
        "pnl_pct":         (final / initial - 1) * 100 if initial else 0,
        "trades":          closed,
        "wins":            wins,
        "losses":          t["losses"],
        "win_rate":        win_rate,
        "best_trade":      float(t["best"]),
        "worst_trade":     float(t["worst"]),
        "start_ts":        start,
        "experiment_days": days,
    }


async def get_equity_curve() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT ts, portfolio_value, btc_buyhold FROM equity ORDER BY ts ASC"
        ) as cur:
            rows = await cur.fetchall()
    return [{"ts": r["ts"], "portfolio": r["portfolio_value"],
             "btc": r["btc_buyhold"]} for r in rows]


async def get_open_trades() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id,symbol,direction,qty,entry_price,sl_price,tp_price,peak_price "
            "FROM trades WHERE status='open'"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_trades(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_daily_performance() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT exit_ts, pnl FROM trades WHERE status='closed' AND exit_ts IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
    daily: dict[str, float] = {}
    for r in rows:
        day = datetime.fromtimestamp(r["exit_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0.0) + float(r["pnl"])
    return [{"day": d, "pnl": v} for d, v in sorted(daily.items())]
