"""
Order execution via Binance REST API (signed requests).
Supports Spot and Cross Margin (LONG + SHORT via AUTO_BORROW_REPAY).
Auto-adjusts quantity to each symbol's LOT_SIZE rules.
"""
import hashlib
import hmac
import math
import time
import logging
import aiohttp
from urllib.parse import urlencode

from config import (BINANCE_API_KEY, BINANCE_API_SECRET,
                    TRADE_AMOUNT_USDC, USE_TESTNET, USE_MARGIN,
                    STOP_LOSS_PCT, TAKE_PROFIT_PCT)
from strategy import Signal, Direction

log = logging.getLogger(__name__)

BASE_URL = "https://testnet.binance.vision" if USE_TESTNET else "https://api.binance.com"

# Cache per lot size e price filter
_lot_size_cache:   dict[str, dict] = {}
_price_filter_cache: dict[str, dict] = {}


def _sign(params: dict) -> str:
    query = urlencode(params)
    return hmac.new(
        BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()


async def _get_lot_size(symbol: str, session: aiohttp.ClientSession) -> dict:
    """Returns LOT_SIZE filter info for a symbol (cached)."""
    if symbol in _lot_size_cache:
        return _lot_size_cache[symbol]

    async with session.get(f"{BASE_URL}/api/v3/exchangeInfo",
                           params={"symbol": symbol},
                           timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json()

    info = {"minQty": 0.0, "stepSize": 0.00001}
    for s in data.get("symbols", []):
        if s["symbol"] == symbol:
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    info = {
                        "minQty":   float(f["minQty"]),
                        "maxQty":   float(f["maxQty"]),
                        "stepSize": float(f["stepSize"]),
                    }
                    break
            break

    _lot_size_cache[symbol] = info
    log.info("LOT_SIZE %s: minQty=%s stepSize=%s",
             symbol, info["minQty"], info["stepSize"])
    return info


async def _get_price_filter(symbol: str, session: aiohttp.ClientSession) -> dict:
    """Returns PRICE_FILTER info (tick size) for a symbol (cached)."""
    if symbol in _price_filter_cache:
        return _price_filter_cache[symbol]

    async with session.get(f"{BASE_URL}/api/v3/exchangeInfo",
                           params={"symbol": symbol},
                           timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json()

    info = {"tickSize": 0.0001, "minPrice": 0.0}
    for s in data.get("symbols", []):
        if s["symbol"] == symbol:
            for f in s.get("filters", []):
                if f["filterType"] == "PRICE_FILTER":
                    info = {
                        "tickSize": float(f["tickSize"]),
                        "minPrice": float(f["minPrice"]),
                    }
                    break
            break

    _price_filter_cache[symbol] = info
    return info


def _round_price(price: float, tick_size: float) -> float:
    """Rounds a price to the nearest valid tick."""
    if tick_size <= 0:
        return price
    precision = max(0, round(-math.log10(tick_size)))
    return round(round(price / tick_size) * tick_size, precision)


def _round_qty(quantity: float, step_size: float, min_qty: float) -> float:
    """Rounds quantity down to the nearest valid step, respecting minimum."""
    if step_size <= 0:
        return quantity
    precision = max(0, round(-math.log10(step_size)))
    qty = math.floor(quantity / step_size) * step_size
    qty = round(qty, precision)
    return max(qty, min_qty)


async def _get_current_price(symbol: str, session: aiohttp.ClientSession) -> float:
    """Fetches the latest price for a symbol."""
    async with session.get(f"{BASE_URL}/api/v3/ticker/price",
                           params={"symbol": symbol},
                           timeout=aiohttp.ClientTimeout(total=5)) as resp:
        data = await resp.json()
        return float(data["price"])


async def place_market_order(symbol: str, side: str, quantity: float) -> dict:
    """
    Places a MARKET order.
    - If USE_MARGIN=true → Cross Margin con AUTO_BORROW_REPAY (supporta LONG e SHORT)
    - Se USE_MARGIN=false → Spot normale (solo LONG)
    """
    async with aiohttp.ClientSession() as session:
        lot = await _get_lot_size(symbol, session)
        qty = _round_qty(quantity, lot["stepSize"], lot["minQty"])
        log.info("Placing %s %s qty=%.8f", side, symbol, qty)

        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}

        if USE_MARGIN:
            # Cross Margin: AUTO_BORROW_REPAY gestisce prestito e rimborso automatico
            url = f"{BASE_URL}/sapi/v1/margin/order"
            params = {
                "symbol":         symbol,
                "side":           side,
                "type":           "MARKET",
                "quantity":       qty,
                "sideEffectType": "AUTO_BORROW_REPAY",
                "timestamp":      int(time.time() * 1000),
            }
        else:
            # Spot standard
            url = f"{BASE_URL}/api/v3/order"
            params = {
                "symbol":    symbol,
                "side":      side,
                "type":      "MARKET",
                "quantity":  qty,
                "timestamp": int(time.time() * 1000),
            }

        params["signature"] = _sign(params)

        async with session.post(url, params=params, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if resp.status != 200:
                log.error("Binance order error %s: %s", resp.status, data)
                raise RuntimeError(f"Binance error {resp.status}: {data.get('msg', data)}")
            log.info("Order placed: %s", data)
            return data


async def place_oco_order(symbol: str, direction: str,
                          qty: float, entry_price: float) -> dict | None:
    """
    Piazza un ordine OCO su Binance Margin dopo l'apertura di una posizione.
    OCO = One-Cancels-the-Other: TP (limit) + SL (stop-limit).
    Quando uno scatta, Binance cancella automaticamente l'altro.

    LONG  → SELL OCO: TP sopra entry, SL sotto entry
    SHORT → BUY  OCO: TP sotto entry, SL sopra entry
    """
    if not USE_MARGIN:
        log.info("OCO non disponibile in modalità Spot — usa solo VPS monitor")
        return None

    async with aiohttp.ClientSession() as session:
        pf       = await _get_price_filter(symbol, session)
        tick     = pf["tickSize"]

        if direction == "LONG":
            close_side = "SELL"
            tp_price   = _round_price(entry_price * (1 + TAKE_PROFIT_PCT / 100), tick)
            sl_stop    = _round_price(entry_price * (1 - STOP_LOSS_PCT  / 100), tick)
            sl_limit   = _round_price(sl_stop * (1 - 0.001), tick)   # leggermente peggio per garanzia fill
        else:  # SHORT
            close_side = "BUY"
            tp_price   = _round_price(entry_price * (1 - TAKE_PROFIT_PCT / 100), tick)
            sl_stop    = _round_price(entry_price * (1 + STOP_LOSS_PCT  / 100), tick)
            sl_limit   = _round_price(sl_stop * (1 + 0.001), tick)

        lot = await _get_lot_size(symbol, session)
        qty = _round_qty(qty, lot["stepSize"], lot["minQty"])

        params = {
            "symbol":              symbol,
            "side":                close_side,
            "quantity":            qty,
            "price":               tp_price,        # limite TP
            "stopPrice":           sl_stop,          # trigger SL
            "stopLimitPrice":      sl_limit,         # limite SL
            "stopLimitTimeInForce": "GTC",
            "sideEffectType":      "AUTO_BORROW_REPAY",
            "timestamp":           int(time.time() * 1000),
        }
        params["signature"] = _sign(params)
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}

        log.info("OCO %s %s: TP=%.4f SL_stop=%.4f SL_limit=%.4f qty=%.6f",
                 close_side, symbol, tp_price, sl_stop, sl_limit, qty)

        async with session.post(f"{BASE_URL}/sapi/v1/margin/order/oco",
                                params=params, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if resp.status != 200:
                log.error("OCO error %s: %s", resp.status, data)
                return None     # OCO fallito → VPS monitor fa da backup
            log.info("OCO placed: listOrderId=%s", data.get("orderListId"))
            return data


async def cancel_open_orders(symbol: str) -> dict | None:
    """
    Cancella TUTTI gli ordini aperti su un simbolo sul conto Cross Margin.

    Serve quando il bot chiude una posizione via market order (es. trailing
    stop): l'OCO originale resta pendente su Binance e potrebbe eseguire
    aprendo una posizione non voluta. Cancellando gli open orders del simbolo
    si evita questo rischio.

    Usa DELETE /sapi/v1/margin/openOrders (symbol + timestamp + signature).
    Ritorna la lista degli ordini cancellati, o None se non applicabile/errore.
    """
    if not USE_MARGIN:
        return None

    async with aiohttp.ClientSession() as session:
        params = {
            "symbol":    symbol,
            "timestamp": int(time.time() * 1000),
        }
        params["signature"] = _sign(params)
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}

        log.info("Cancelling open margin orders for %s", symbol)

        async with session.delete(f"{BASE_URL}/sapi/v1/margin/openOrders",
                                  params=params, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if resp.status != 200:
                log.error("Cancel open orders error %s: %s", resp.status, data)
                return None
            log.info("Cancelled %d open order(s) for %s",
                     len(data) if isinstance(data, list) else 0, symbol)
            return data


async def place_order(signal: Signal) -> dict:
    """
    Places a MARKET order based on signal direction.
    Quantity = TRADE_AMOUNT_USDC / prezzo corrente.
    """
    side = "BUY" if signal.direction == Direction.LONG else "SELL"

    async with aiohttp.ClientSession() as session:
        price   = await _get_current_price(signal.symbol, session)
        lot     = await _get_lot_size(signal.symbol, session)
        raw_qty = TRADE_AMOUNT_USDC / price
        qty     = _round_qty(raw_qty, lot["stepSize"], lot["minQty"])
        log.info("Order calc: %.2f USDC / %.4f = %.6f → rounded %.6f",
                 TRADE_AMOUNT_USDC, price, raw_qty, qty)

    return await place_market_order(signal.symbol, side, qty)


def avg_fill_price(order: dict, fallback: float) -> float:
    """Computes the volume-weighted average fill price of an order."""
    fills = order.get("fills", [])
    if not fills:
        return fallback
    total_qty = sum(float(f["qty"]) for f in fills)
    if total_qty == 0:
        return fallback
    return sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty


async def get_account_balance(asset: str = "USDC") -> float:
    """
    Returns free balance for `asset`.
    Legge dal conto Margin se USE_MARGIN=true, altrimenti Spot.
    """
    params = {"timestamp": int(time.time() * 1000)}
    params["signature"] = _sign(params)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}

    async with aiohttp.ClientSession() as session:
        if USE_MARGIN:
            url = f"{BASE_URL}/sapi/v1/margin/account"
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                for b in data.get("userAssets", []):
                    if b["asset"] == asset:
                        return float(b["free"])
        else:
            url = f"{BASE_URL}/api/v3/account"
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                for b in data.get("balances", []):
                    if b["asset"] == asset:
                        return float(b["free"])
    return 0.0
