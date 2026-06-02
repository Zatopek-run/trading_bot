"""
Fetches OHLCV candles from Binance REST API (no auth required).
Returns a pandas DataFrame ready for strategy analysis.
"""
import asyncio
import aiohttp
import pandas as pd
import logging
from config import TIMEFRAME, CANDLES_LIMIT

log = logging.getLogger(__name__)

BINANCE_BASE = "https://api.binance.com"
TESTNET_BASE = "https://testnet.binance.vision"


async def fetch_klines(symbol: str, interval: str = TIMEFRAME,
                       limit: int = CANDLES_LIMIT,
                       session: aiohttp.ClientSession = None) -> pd.DataFrame:
    """
    Fetches candlestick data for `symbol` from Binance public REST.
    Returns DataFrame with columns: open_time, open, high, low, close, volume
    """
    url = f"{BINANCE_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            raw = await resp.json()
    finally:
        if own_session:
            await session.close()

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df[["open_time", "open", "high", "low", "close", "volume"]]


async def fetch_ticker_price(symbol: str,
                              session: aiohttp.ClientSession = None) -> float:
    """Returns the latest price for a symbol."""
    url = f"{BINANCE_BASE}/api/v3/ticker/price"
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        async with session.get(url, params={"symbol": symbol},
                               timeout=aiohttp.ClientTimeout(total=5)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return float(data["price"])
    finally:
        if own_session:
            await session.close()


async def fetch_all_symbols(symbols: list[str],
                             interval: str = TIMEFRAME,
                             limit: int = CANDLES_LIMIT) -> dict[str, pd.DataFrame]:
    """Fetches candles for multiple symbols concurrently."""
    async with aiohttp.ClientSession() as session:
        tasks = {sym: fetch_klines(sym, interval, limit, session) for sym in symbols}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    out = {}
    for sym, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            log.warning("Failed to fetch %s: %s", sym, result)
        else:
            out[sym] = result
    return out
