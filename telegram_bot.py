"""
Telegram bot: sends trading signals with inline buttons.
Esegui → places order on Binance.
Ignora → dismisses the signal.
"""
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CallbackQueryHandler,
                           CommandHandler, ContextTypes)

import config
from config import (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TRADE_QUANTITY, SPOT_ONLY, AUTO_TRADE,
                    INITIAL_CAPITAL, MAX_OPEN_TRADES)
from strategy import Signal, Direction
from trader import place_order, get_account_balance, place_market_order, avg_fill_price, place_oco_order, cancel_open_orders
from database import record_order, get_open_trades, realized_pnl
from analyzer import fetch_ticker_price

log = logging.getLogger(__name__)

# Pending signals waiting for user confirmation: callback_data_key → Signal
_pending: dict[str, Signal] = {}


def _make_keyboard(signal_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Esegui",  callback_data=f"exec:{signal_id}"),
        InlineKeyboardButton("❌ Ignora",  callback_data=f"skip:{signal_id}"),
    ]])


async def send_text(app: Application, text: str) -> None:
    """Sends a plain notification (e.g. SL/TP auto-close) to the chat."""
    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text,
                               parse_mode="Markdown")


async def send_signal(app: Application, signal: Signal) -> None:
    """Push a signal notification to the Telegram chat."""
    import uuid
    signal_id = str(uuid.uuid4())[:8]
    _pending[signal_id] = signal

    text = (
        f"🔔 *NUOVO SEGNALE*\n\n"
        f"{signal}\n\n"
        f"_ID: {signal_id}_"
    )
    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode="Markdown",
        reply_markup=_make_keyboard(signal_id),
    )
    log.info("Signal sent to Telegram: %s %s", signal.symbol, signal.direction.value)


async def execute_auto_trade(app: Application, signal: Signal) -> None:
    """Esegue automaticamente un ordine e notifica su Telegram (usato quando AUTO_TRADE=true)."""
    if SPOT_ONLY and signal.direction == Direction.SHORT:
        await send_text(
            app,
            f"⚠️ *Auto-trade ignorato* — SHORT non disponibile in modalità Spot\n"
            f"{signal.symbol} {signal.direction.value}",
        )
        return

    log.info("AUTO_TRADE: placing order %s %s", signal.symbol, signal.direction.value)
    try:
        order = await place_order(signal)
        fills = order.get("fills", [])
        avg_px = (sum(float(f["price"]) * float(f["qty"]) for f in fills)
                  / sum(float(f["qty"]) for f in fills)) if fills else signal.price
        executed_qty = float(order.get("executedQty", TRADE_QUANTITY)) or TRADE_QUANTITY

        trade = await record_order(
            symbol=order["symbol"], side=order["side"], qty=executed_qty,
            price=avg_px, order_id=order["orderId"], score=signal.score,
        )

        ledger = ""
        if trade["action"] == "close":
            # Il market order ha chiuso una posizione esistente: cancella l'OCO
            # pendente sul simbolo per evitare che riapra una posizione.
            try:
                await cancel_open_orders(order["symbol"])
            except Exception:
                log.exception("cancel_open_orders failed for %s", order["symbol"])
            emoji = "🟢" if trade["pnl"] >= 0 else "🔴"
            ledger = f"\n{emoji} Trade chiuso — PnL: {trade['pnl']:+.2f} ({trade['pnl_pct']:+.2f}%)"
        else:
            sl = trade.get("sl_price", 0)
            tp = trade.get("tp_price", 0)
            ledger = (f"\n📌 Posizione {trade['direction']} aperta\n"
                      f"🛑 SL: {sl:.4f}  🎯 TP: {tp:.4f}")
            oco = await place_oco_order(
                symbol=order["symbol"],
                direction=trade["direction"],
                qty=executed_qty,
                entry_price=avg_px,
            )
            ledger += "\n✅ OCO impostato su Binance" if oco else "\n⚠️ OCO non impostato — SL/TP gestito dal VPS"

        await send_text(
            app,
            f"🤖 *Trade automatico aperto*\n\n"
            f"Symbol: `{order['symbol']}`\n"
            f"Direzione: *{signal.direction.value}*\n"
            f"Prezzo: `{avg_px:.4f}`\n"
            f"Qty: `{executed_qty}`\n"
            f"Score: {signal.score} confluenze"
            f"{ledger}",
        )
    except Exception as exc:
        log.exception("AUTO_TRADE order failed for %s", signal.symbol)
        await send_text(app, f"🚨 *Errore auto-trade {signal.symbol}*\n`{exc}`")


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    action, payload = query.data.split(":", 1)

    # Gestione closeall
    if action == "closeall":
        await _handle_closeall(query, payload)
        return

    signal_id = payload
    signal = _pending.pop(signal_id, None)

    if signal is None:
        await query.edit_message_text("⚠️ Segnale già gestito o scaduto.")
        return

    if action == "skip":
        await query.edit_message_text(
            f"❌ *Segnale ignorato*\n{signal.symbol} {signal.direction.value}",
            parse_mode="Markdown",
        )
        return

    # action == "exec"
    if SPOT_ONLY and signal.direction == Direction.SHORT:
        await query.edit_message_text(
            "⚠️ *Segnale SHORT ignorato*\n"
            "In modalità Spot si eseguono solo i LONG.\n"
            "Abilita Futures per fare short.",
            parse_mode="Markdown",
        )
        return

    await query.edit_message_text(
        f"⏳ Eseguendo ordine {signal.direction.value} su {signal.symbol}…",
        parse_mode="Markdown",
    )
    try:
        order = await place_order(signal)
        fills  = order.get("fills", [])
        avg_px = (sum(float(f["price"]) * float(f["qty"]) for f in fills)
                  / sum(float(f["qty"]) for f in fills)) if fills else signal.price
        executed_qty = float(order.get("executedQty", TRADE_QUANTITY)) or TRADE_QUANTITY

        trade = await record_order(
            symbol=order["symbol"], side=order["side"], qty=executed_qty,
            price=avg_px, order_id=order["orderId"], score=signal.score,
        )
        ledger = ""
        if trade["action"] == "close":
            # Il market order ha chiuso una posizione esistente: cancella l'OCO
            # pendente sul simbolo per evitare che riapra una posizione.
            try:
                await cancel_open_orders(order["symbol"])
            except Exception:
                log.exception("cancel_open_orders failed for %s", order["symbol"])
            emoji = "🟢" if trade["pnl"] >= 0 else "🔴"
            ledger = f"\n{emoji} Trade chiuso — PnL: {trade['pnl']:+.2f} ({trade['pnl_pct']:+.2f}%)"
        else:
            sl = trade.get("sl_price", 0)
            tp = trade.get("tp_price", 0)
            ledger = (f"\n📌 Posizione {trade['direction']} aperta\n"
                      f"🛑 SL: {sl:.4f}  🎯 TP: {tp:.4f}")
            # Piazza OCO su Binance come protezione hardware
            oco = await place_oco_order(
                symbol=order["symbol"],
                direction=trade["direction"],
                qty=executed_qty,
                entry_price=avg_px,
            )
            if oco:
                ledger += "\n✅ OCO impostato su Binance"
            else:
                ledger += "\n⚠️ OCO non impostato — SL/TP gestito dal VPS"

        await query.edit_message_text(
            f"✅ *Ordine eseguito!*\n"
            f"Symbol: {order['symbol']}\n"
            f"Side: {order['side']}\n"
            f"Qty: {order['executedQty']}\n"
            f"Prezzo medio: {avg_px:.4f}\n"
            f"OrderId: `{order['orderId']}`"
            f"{ledger}",
            parse_mode="Markdown",
        )
    except Exception as exc:
        log.exception("Order failed")
        await query.edit_message_text(
            f"🚨 *Errore nell'ordine*\n`{exc}`",
            parse_mode="Markdown",
        )


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 *Trading Bot attivo*\n\n"
        "Riceverai i segnali automaticamente.\n"
        "Usa /balance per vedere il tuo saldo.",
        parse_mode="Markdown",
    )


async def _cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        usdc = await get_account_balance("USDC")
        btc  = await get_account_balance("BTC")
        await update.message.reply_text(
            f"💰 *Saldo*\nUSDC: {usdc:.2f}\nBTC: {btc:.8f}",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Errore: {exc}")


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending_count = len(_pending)
    await update.message.reply_text(
        f"📊 *Stato bot*\nSegnali in attesa: {pending_count}",
        parse_mode="Markdown",
    )


async def _cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        trades = await get_open_trades()
        if not trades:
            await update.message.reply_text(
                "📭 *Nessuna posizione aperta*", parse_mode="Markdown"
            )
            return

        lines = ["📋 *Posizioni aperte*\n"]
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
            total_pnl += pnl

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
        lines.append(f"\n{total_emoji} *Totale non realizzato: {total_pnl:+.2f} USDC*")

        await update.message.reply_text(
            "\n\n".join(lines), parse_mode="Markdown"
        )
    except Exception as exc:
        await update.message.reply_text(f"Errore: {exc}")


async def _cmd_closeall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chiede conferma prima di chiudere tutte le posizioni aperte."""
    trades = await get_open_trades()
    if not trades:
        await update.message.reply_text(
            "📭 *Nessuna posizione aperta da chiudere.*", parse_mode="Markdown"
        )
        return

    symbols = ", ".join(t["symbol"] for t in trades)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sì, chiudi tutto", callback_data="closeall:confirm"),
        InlineKeyboardButton("❌ Annulla",          callback_data="closeall:cancel"),
    ]])
    await update.message.reply_text(
        f"⚠️ *Vuoi chiudere tutte le {len(trades)} posizioni aperte?*\n\n"
        f"_{symbols}_\n\n"
        f"Verranno piazzati ordini di mercato immediati.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def _handle_closeall(query, action: str) -> None:
    if action == "cancel":
        await query.edit_message_text("❌ *Chiusura annullata.*", parse_mode="Markdown")
        return

    await query.edit_message_text("⏳ *Chiusura posizioni in corso…*", parse_mode="Markdown")

    trades = await get_open_trades()
    if not trades:
        await query.edit_message_text("📭 *Nessuna posizione aperta.*", parse_mode="Markdown")
        return

    results = []
    total_pnl = 0.0

    for t in trades:
        close_side = "BUY" if t["direction"] == "SHORT" else "SELL"
        try:
            order   = await place_market_order(t["symbol"], close_side, t["qty"])
            fill_px = avg_fill_price(order, t["entry_price"])
            # Chiusura via market order: cancella l'OCO pendente sul simbolo
            # per evitare che esegua aprendo una posizione non voluta.
            try:
                await cancel_open_orders(t["symbol"])
            except Exception:
                log.exception("cancel_open_orders failed for %s", t["symbol"])
            closed  = await record_order(
                symbol=t["symbol"], side=close_side, qty=t["qty"],
                price=fill_px, order_id=order["orderId"], score=0,
                close_reason="manual_closeall",
            )
            pnl     = closed.get("pnl", 0)
            pnl_pct = closed.get("pnl_pct", 0)
            total_pnl += pnl
            emoji = "🟢" if pnl >= 0 else "🔴"
            results.append(
                f"{emoji} *{t['symbol']}* chiuso a `{fill_px:.4f}`\n"
                f"   PnL: `{pnl:+.2f} USDC` ({pnl_pct:+.2f}%)"
            )
        except Exception as exc:
            log.exception("Errore chiusura %s", t["symbol"])
            results.append(f"🚨 *{t['symbol']}* — errore: `{exc}`")

    total_emoji = "🟢" if total_pnl >= 0 else "🔴"
    summary = "\n\n".join(results)
    await query.edit_message_text(
        f"✅ *Chiusura completata*\n\n{summary}\n\n"
        f"{total_emoji} *PnL totale: {total_pnl:+.2f} USDC*",
        parse_mode="Markdown",
    )


async def _cmd_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        capitale = INITIAL_CAPITAL + await realized_pnl()
        size     = config.TRADE_AMOUNT_USDC
        esposizione = size * MAX_OPEN_TRADES
        pct = esposizione / capitale * 100 if capitale else 0.0
        await update.message.reply_text(
            f"📐 *Trade size attuale:* `{size} USDC`\n"
            f"Capitale stimato: `{capitale:.2f} USDC`\n"
            f"Esposizione max: `{esposizione} USDC`\n"
            f"({pct:.1f}% del capitale)",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Errore: {exc}")


def build_app() -> Application:
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .build())
    app.add_handler(CommandHandler("start",     _cmd_start))
    app.add_handler(CommandHandler("balance",   _cmd_balance))
    app.add_handler(CommandHandler("status",    _cmd_status))
    app.add_handler(CommandHandler("positions", _cmd_positions))
    app.add_handler(CommandHandler("closeall",  _cmd_closeall))
    app.add_handler(CommandHandler("limit",     _cmd_limit))
    app.add_handler(CallbackQueryHandler(_handle_callback))
    return app
