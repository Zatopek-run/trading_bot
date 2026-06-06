"""
Entry point: runs the market scanner loop + Telegram bot concurrently.
"""
import asyncio
import logging
import signal as os_signal
import sys
import time

import aiosqlite
from telegram.ext import Application

import config
from config import (SYMBOLS_TO_SCAN, SCAN_INTERVAL_SEC, MONITOR_INTERVAL_SEC,
                    TIMEFRAME, CANDLES_LIMIT, BENCHMARK_SYMBOL,
                    INITIAL_CAPITAL, ENABLE_SL_TP, MAX_OPEN_TRADES,
                    POSITIONS_REPORT_INTERVAL_SEC, AUTO_TRADE, REGIME_FILTER,
                    TRADE_AMOUNT_USDC, STOP_LOSS_PCT, TAKE_PROFIT_PCT, DB_PATH,
                    ENABLE_TRAILING_STOP, TRAILING_STOP_PCT, TRAILING_ACTIVATION_PCT)
from analyzer import fetch_all_symbols, fetch_ticker_price
from strategy import analyze, Direction
from telegram_bot import build_app, send_signal, send_text, execute_auto_trade
from trader import place_market_order, avg_fill_price, cancel_open_orders
from database import (init_db, record_equity, realized_pnl,
                      record_order, get_open_trades, get_meta, set_meta,
                      get_trades, update_trailing_sl)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trading_bot.log", encoding="utf-8"),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


async def _snapshot_equity() -> None:
    """
    Records portfolio value including:
    - Realized PnL (trade chiusi)
    - Unrealized PnL (posizioni aperte al prezzo corrente)
    - BTC buy&hold come benchmark
    """
    try:
        btc_now = await fetch_ticker_price(BENCHMARK_SYMBOL)
        start_px = await get_meta("btc_start_price")
        if start_px is None:
            await set_meta("btc_start_price", str(btc_now))
            start_px = btc_now
        start_px = float(start_px)

        # PnL realizzato (trade chiusi)
        realized = await realized_pnl()

        # PnL non realizzato (posizioni aperte)
        open_trades = await get_open_trades()
        unrealized = 0.0
        for t in open_trades:
            try:
                current_price = await fetch_ticker_price(t["symbol"])
                if t["direction"] == "LONG":
                    unrealized += (current_price - t["entry_price"]) * t["qty"]
                else:  # SHORT
                    unrealized += (t["entry_price"] - current_price) * t["qty"]
            except Exception:
                pass

        portfolio = INITIAL_CAPITAL + realized + unrealized
        btc_buyhold = INITIAL_CAPITAL * (btc_now / start_px) if start_px else INITIAL_CAPITAL
        await record_equity(portfolio, btc_buyhold)
        log.debug("Equity snapshot: realized=%.2f unrealized=%.2f total=%.2f",
                  realized, unrealized, portfolio)
    except Exception:
        log.exception("Equity snapshot failed")


async def monitor_positions(app: Application) -> None:
    """
    Checks open positions against their SL/TP levels and auto-closes
    via a market order when the current price crosses either threshold.
    """
    if not ENABLE_SL_TP:
        return

    open_trades = await get_open_trades()
    for t in open_trades:
        try:
            price = await fetch_ticker_price(t["symbol"])
        except Exception:
            log.warning("Price fetch failed for %s during monitor", t["symbol"])
            continue

        if ENABLE_TRAILING_STOP:
            peak = t.get("peak_price") or t["entry_price"]
            entry = t["entry_price"]
            if t["direction"] == "LONG":
                activation_price = entry * (1 + TRAILING_ACTIVATION_PCT / 100)
                trailing_active = price >= activation_price
                if trailing_active:
                    new_peak = max(peak, price)
                    new_trail_sl = new_peak * (1 - TRAILING_STOP_PCT / 100)
                    current_sl = t["sl_price"] or 0.0
                    new_sl = max(new_trail_sl, current_sl)
                    sl_improved = new_sl > current_sl
                else:
                    new_peak = peak
                    new_sl = t["sl_price"] or 0.0
                    sl_improved = False
            else:  # SHORT
                activation_price = entry * (1 - TRAILING_ACTIVATION_PCT / 100)
                trailing_active = price <= activation_price
                if trailing_active:
                    new_peak = min(peak, price)
                    new_trail_sl = new_peak * (1 + TRAILING_STOP_PCT / 100)
                    current_sl = t["sl_price"] or float("inf")
                    new_sl = min(new_trail_sl, current_sl)
                    sl_improved = t["sl_price"] is None or new_sl < t["sl_price"]
                else:
                    new_peak = peak
                    new_sl = t["sl_price"] or float("inf")
                    sl_improved = False

            if new_peak != peak or sl_improved:
                await update_trailing_sl(t["id"], new_sl, new_peak)
                t["sl_price"] = new_sl
                if sl_improved:
                    arrow = "📈" if t["direction"] == "LONG" else "📉"

                    def _guaranteed(trade):
                        sl = trade["sl_price"]
                        ep = trade["entry_price"]
                        qty = trade["qty"]
                        if sl is None:
                            return 0.0
                        if trade["direction"] == "LONG":
                            return (sl - ep) * qty
                        return (ep - sl) * qty

                    def _pct(trade):
                        ep = trade["entry_price"]
                        qty = trade["qty"]
                        invested = ep * qty
                        if invested == 0:
                            return 0.0
                        return _guaranteed(trade) / invested * 100

                    this_profit = _guaranteed(t)
                    this_pct = _pct(t)
                    profit_sign = "+" if this_profit >= 0 else ""

                    lines = [
                        f"{arrow} *Trailing SL aggiornato*",
                        f"{t['symbol']} {t['direction']}",
                        f"  Nuovo SL: `{new_sl:.4f}`  (peak: `{new_peak:.4f}`)",
                        f"  Profitto garantito: `{profit_sign}{this_profit:.2f} USDC` ({profit_sign}{this_pct:.2f}%)",
                        "",
                        "*Riepilogo posizioni aperte (SL attuali):*",
                    ]
                    total_guaranteed = 0.0
                    fresh_trades = await get_open_trades()
                    for pos in fresh_trades:
                        if pos["id"] == t["id"]:
                            pos = dict(pos, sl_price=new_sl)
                        pos_sl = pos["sl_price"]
                        if pos_sl is None:
                            continue
                        g = _guaranteed(pos)
                        p = _pct(pos)
                        total_guaranteed += g
                        g_sign = "+" if g >= 0 else ""
                        lines.append(
                            f"  • {pos['symbol']} {pos['direction']}  SL `{pos_sl:.4f}`"
                            f"  →  `{g_sign}{g:.2f} USDC` ({g_sign}{p:.2f}%)"
                        )
                    t_sign = "+" if total_guaranteed >= 0 else ""
                    lines.append(f"\n*Totale garantito: `{t_sign}{total_guaranteed:.2f} USDC`*")

                    await send_text(app, "\n".join(lines))

        sl, tp = t["sl_price"], t["tp_price"]
        if sl is None or tp is None:
            continue

        hit = None
        if t["direction"] == "LONG":
            if price <= sl:   hit = "stop_loss"
            elif price >= tp: hit = "take_profit"
        else:  # SHORT
            if price >= sl:   hit = "stop_loss"
            elif price <= tp: hit = "take_profit"

        if hit is None:
            continue

        close_side = "SELL" if t["direction"] == "LONG" else "BUY"
        log.info("%s on %s %s → closing (price %.4f)",
                 hit, t["symbol"], t["direction"], price)
        try:
            order = await place_market_order(t["symbol"], close_side, t["qty"])
            fill_px = avg_fill_price(order, price)
            # La posizione è stata chiusa con un market order (es. trailing stop):
            # l'OCO originale resta pendente su Binance e potrebbe eseguire
            # aprendo una posizione non voluta. Cancelliamo gli ordini pendenti
            # sul simbolo prima di registrare la chiusura.
            try:
                await cancel_open_orders(t["symbol"])
            except Exception:
                log.exception("cancel_open_orders failed for %s", t["symbol"])
            closed = await record_order(
                symbol=t["symbol"], side=close_side, qty=t["qty"],
                price=fill_px, order_id=order["orderId"], score=0,
                close_reason=hit,
            )
            emoji = "🟢" if closed.get("pnl", 0) >= 0 else "🔴"
            label = "🎯 Take-Profit" if hit == "take_profit" else "🛑 Stop-Loss"
            await send_text(
                app,
                f"{label} *automatico*\n"
                f"{t['symbol']} {t['direction']} chiuso a {fill_px:.4f}\n"
                f"{emoji} PnL: {closed.get('pnl',0):+.2f} "
                f"({closed.get('pnl_pct',0):+.2f}%)",
            )
        except Exception as exc:
            log.exception("Auto-close failed for %s", t["symbol"])
            await send_text(app, f"🚨 Errore chiusura automatica {t['symbol']}: `{exc}`")


def _btc_below_ema50(data: dict) -> bool:
    """Regime check: True se BTC chiude sotto la sua EMA50 (mercato ribassista).

    Usa i dati già scaricati nel loop; ritorna False se BTC manca o ha
    troppo poche candele per una EMA50 sensata.
    """
    df = data.get(BENCHMARK_SYMBOL)
    if df is None or len(df) < 50:
        return False
    ema50 = df["close"].ewm(span=50, adjust=False).mean()
    return float(df["close"].iloc[-1]) < float(ema50.iloc[-1])


def _btc_above_ema50(data: dict) -> bool:
    """Regime check: True se BTC chiude sopra la sua EMA50 (mercato rialzista)."""
    df = data.get(BENCHMARK_SYMBOL)
    if df is None or len(df) < 50:
        return False
    ema50 = df["close"].ewm(span=50, adjust=False).mean()
    return float(df["close"].iloc[-1]) > float(ema50.iloc[-1])


async def _record_simulated(sig, direction: str = "LONG") -> tuple[float, float]:
    """Inserisce un trade simulato (paper trading) in 'trades' senza eseguire l'ordine.

    Ritorna (sl_price, tp_price) per la notifica. status='simulated',
    close_reason=None; SL/TP calcolati come per un trade reale.
    """
    entry = sig.price
    qty   = TRADE_AMOUNT_USDC / entry if entry else 0.0
    if direction == "SHORT":
        sl = entry * (1 + STOP_LOSS_PCT / 100)
        tp = entry * (1 - TAKE_PROFIT_PCT / 100)
    else:
        sl = entry * (1 - STOP_LOSS_PCT / 100)
        tp = entry * (1 + TAKE_PROFIT_PCT / 100)
    now   = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO trades(symbol,direction,qty,entry_price,entry_ts,"
            "status,sl_price,tp_price,peak_price,close_reason) "
            "VALUES(?,?,?,?,?, 'simulated',?,?,?,NULL)",
            (sig.symbol, direction, qty, entry, now, sl, tp, entry),
        )
        await db.commit()
    return sl, tp


async def _update_trade_size_if_needed(app: Application) -> None:
    """Ricalibra TRADE_AMOUNT_USDC se il capitale si è mosso di >=10%."""
    global TRADE_AMOUNT_USDC
    try:
        capitale = INITIAL_CAPITAL + await realized_pnl()
        last_str = await get_meta("last_size_capital")

        if last_str is None:
            # Prima esecuzione: inizializza e applica subito la size corretta.
            await set_meta("last_size_capital", str(capitale))
            last_str = str(capitale)
            diff_pct = 100.0  # forza l'aggiornamento al primo avvio
        else:
            last_capitale = float(last_str)
            diff_pct = (abs(capitale - last_capitale) / last_capitale * 100
                        if last_capitale else 100.0)

        if diff_pct >= 10:
            vecchia_size = TRADE_AMOUNT_USDC
            nuova_size   = int((capitale * 0.90) / MAX_OPEN_TRADES)
            config.TRADE_AMOUNT_USDC = nuova_size
            TRADE_AMOUNT_USDC        = nuova_size
            await set_meta("last_size_capital", str(capitale))
            await send_text(
                app,
                f"📐 Trade size aggiornato\n"
                f"Capitale: {capitale:.2f} USDC\n"
                f"Nuova size: {nuova_size} USDC\n"
                f"(era {vecchia_size} USDC)"
            )
            log.info("Trade size aggiornato: %d → %d USDC (capitale: %.2f)",
                     vecchia_size, nuova_size, capitale)
    except Exception:
        log.exception("_update_trade_size_if_needed failed")


async def scanner_loop(app: Application) -> None:
    """Continuously scans all symbols and fires signals when strategy triggers."""
    log.info("Scanner started — watching %s on %s", SYMBOLS_TO_SCAN, TIMEFRAME)
    scan_count = 0
    while True:
        try:
            scan_count += 1
            await _update_trade_size_if_needed(app)
            open_trades = await get_open_trades()
            open_count  = len(open_trades)
            open_symbols = {t["symbol"] for t in open_trades}

            data = await fetch_all_symbols(SYMBOLS_TO_SCAN, TIMEFRAME, CANDLES_LIMIT)
            found = []
            for symbol, df in data.items():
                # Skip se abbiamo già una posizione aperta su questo simbolo
                if symbol in open_symbols:
                    continue
                # Skip se abbiamo raggiunto il massimo di trade aperti
                if open_count >= MAX_OPEN_TRADES:
                    log.info("MAX_OPEN_TRADES (%d) raggiunto — segnali sospesi",
                             MAX_OPEN_TRADES)
                    break
                sig = analyze(symbol, df)
                if sig:
                    found.append(f"{symbol}({sig.score})")
                    log.info("Signal: %s %s (score %d)", symbol, sig.direction.value, sig.score)
                    if (REGIME_FILTER and sig.direction == Direction.LONG
                            and _btc_below_ema50(data)):
                        # Regime ribassista: niente ordine reale, solo paper trading.
                        sl, tp = await _record_simulated(sig, "LONG")
                        await send_text(
                            app,
                            f"📊 LONG simulato (regime ribassista)\n"
                            f"{sig.symbol} entry: {sig.price}\n"
                            f"SL: {sl} TP: {tp}"
                        )
                        log.info("LONG simulato (regime ribassista): %s", symbol)
                    elif (REGIME_FILTER and sig.direction == Direction.SHORT
                            and _btc_above_ema50(data)):
                        # Regime rialzista: niente ordine SHORT reale, solo paper trading.
                        sl, tp = await _record_simulated(sig, "SHORT")
                        await send_text(
                            app,
                            f"📊 SHORT simulato (regime rialzista)\n"
                            f"{sig.symbol} entry: {sig.price}\n"
                            f"SL: {sl} TP: {tp}"
                        )
                        log.info("SHORT simulato (regime rialzista): %s", symbol)
                    elif AUTO_TRADE:
                        await execute_auto_trade(app, sig)
                    else:
                        await send_signal(app, sig)
                    open_count += 1   # conta il segnale inviato come potenziale trade
            regime = "BEARISH" if _btc_below_ema50(data) else "BULLISH"
            log.info("Scan #%d — regime: %s — %d simboli analizzati — aperti: %d/%d — segnali: %s",
                     scan_count, regime, len(data), len(open_trades), MAX_OPEN_TRADES,
                     ", ".join(found) if found else "nessuno")
            await _snapshot_equity()
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Scanner error — will retry in %ds", SCAN_INTERVAL_SEC)

        await asyncio.sleep(SCAN_INTERVAL_SEC)


async def monitor_loop(app: Application) -> None:
    """Fast loop that watches open positions for SL/TP hits."""
    if not ENABLE_SL_TP:
        log.info("SL/TP disabled — monitor loop not started")
        return
    log.info("SL/TP monitor started (every %ds)", MONITOR_INTERVAL_SEC)
    while True:
        try:
            await monitor_positions(app)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Monitor error")
        await asyncio.sleep(MONITOR_INTERVAL_SEC)


async def positions_report_loop(app: Application) -> None:
    """Invia automaticamente il report posizioni ogni POSITIONS_REPORT_INTERVAL_SEC."""
    log.info("Report posizioni automatico ogni %dh", POSITIONS_REPORT_INTERVAL_SEC // 3600)
    await asyncio.sleep(POSITIONS_REPORT_INTERVAL_SEC)   # prima pausa prima del primo invio
    while True:
        try:
            trades = await get_open_trades()
            if trades:
                lines = [f"🕐 *Report automatico — {len(trades)} posizioni aperte*\n"]
                total_pnl = 0.0
                for t in trades:
                    try:
                        price = await fetch_ticker_price(t["symbol"])
                    except Exception:
                        price = t["entry_price"]
                    if t["direction"] == "LONG":
                        pnl = (price - t["entry_price"]) * t["qty"]
                    else:
                        pnl = (t["entry_price"] - price) * t["qty"]
                    cost    = t["entry_price"] * t["qty"]
                    pnl_pct = (pnl / cost * 100) if cost else 0
                    total_pnl  += pnl
                    emoji      = "🟢" if pnl >= 0 else "🔴"
                    dir_label  = "📈 LONG" if t["direction"] == "LONG" else "📉 SHORT"
                    value_usdc = price * t["qty"]
                    lines.append(
                        f"{emoji} *{t['symbol']}* — {dir_label}\n"
                        f"  Entry: `{t['entry_price']:.4f}` → Now: `{price:.4f}`\n"
                        f"  Qty: `{t['qty']}` — Valore: `{value_usdc:.2f} USDC`\n"
                        f"  PnL: `{pnl:+.2f} USDC` ({pnl_pct:+.2f}%)\n"
                        f"  🛑 SL: `{t['sl_price']:.4f}`  🎯 TP: `{t['tp_price']:.4f}`"
                    )
                total_emoji = "🟢" if total_pnl >= 0 else "🔴"
                lines.append(f"\n{total_emoji} *Totale: {total_pnl:+.2f} USDC*")
                await send_text(app, "\n\n".join(lines))
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Errore report posizioni automatico")

        await asyncio.sleep(POSITIONS_REPORT_INTERVAL_SEC)


async def main() -> None:
    await init_db()
    app = build_app()

    scanner_task = asyncio.create_task(scanner_loop(app))
    monitor_task = asyncio.create_task(monitor_loop(app))
    report_task  = asyncio.create_task(positions_report_loop(app))

    def _cancel_all():
        scanner_task.cancel()
        monitor_task.cancel()
        report_task.cancel()

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _cancel_all)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler for all signals

    log.info("Starting Telegram bot…")
    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        log.info("Bot running. Press Ctrl+C to stop.")
        try:
            await asyncio.gather(scanner_task, monitor_task, report_task)
        except asyncio.CancelledError:
            log.info("Loops stopped.")

        await app.updater.stop()
        await app.stop()

    log.info("Bot stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
