"""
Strategy engine: RSI, MACD, Volume confirmation, Candlestick patterns, EMA.
All indicators implemented directly with pandas/numpy — no extra dependencies.
Returns Signal objects when enough confluences align.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from config import (RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT,
                    MACD_FAST, MACD_SLOW, MACD_SIGNAL_PERIOD,
                    VOLUME_MA_PERIOD, VOLUME_THRESHOLD)

log = logging.getLogger(__name__)


class Direction(Enum):
    LONG  = "LONG"
    SHORT = "SHORT"


@dataclass
class Signal:
    symbol:    str
    direction: Direction
    price:     float
    rsi:       float
    macd:      float
    macd_hist: float
    volume_ratio: float
    pattern:   str
    reasons:   list[str] = field(default_factory=list)
    score:     int = 0

    def __str__(self) -> str:
        arrow = "📈 LONG" if self.direction == Direction.LONG else "📉 SHORT"
        reasons_text = "\n  • ".join(self.reasons)
        return (
            f"{arrow}  {self.symbol}\n"
            f"  Prezzo: {self.price:.4f}\n"
            f"  RSI: {self.rsi:.1f}\n"
            f"  MACD hist: {self.macd_hist:.6f}\n"
            f"  Volume: {self.volume_ratio:.1f}x media\n"
            f"  Pattern: {self.pattern}\n"
            f"  Confluenze ({self.score}/5):\n  • {reasons_text}"
        )


# ── Indicators ───────────────────────────────────────────────────────────────

def _rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series,
          fast: int = MACD_FAST,
          slow: int = MACD_SLOW,
          signal: int = MACD_SIGNAL_PERIOD):
    ema_fast    = close.ewm(span=fast,   adjust=False).mean()
    ema_slow    = close.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram


def _ema(close: pd.Series, period: int = 50) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def _volume_ratio(volume: pd.Series) -> float:
    ma = volume.rolling(VOLUME_MA_PERIOD).mean().iloc[-1]
    if ma == 0:
        return 0.0
    return float(volume.iloc[-1] / ma)


# ── Candlestick patterns ──────────────────────────────────────────────────────

def _detect_pattern(df: pd.DataFrame) -> str:
    if len(df) < 3:
        return "none"

    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values

    def body(i):     return abs(c[i] - o[i])
    def candle(i):   return h[i] - l[i]
    def is_bull(i):  return c[i] > o[i]
    def is_bear(i):  return c[i] < o[i]

    i = -1
    lower_shadow = (o[i] - l[i]) if is_bull(i) else (c[i] - l[i])
    upper_shadow = (h[i] - c[i]) if is_bull(i) else (h[i] - o[i])
    b = body(i)

    # Hammer
    if b > 0 and lower_shadow > 2 * b and upper_shadow < b * 0.3:
        return "hammer"

    # Shooting star
    if b > 0 and upper_shadow > 2 * b and lower_shadow < b * 0.3:
        return "shooting_star"

    # Bullish engulfing
    if is_bear(-2) and is_bull(i) and c[i] > o[-2] and o[i] < c[-2]:
        return "bullish_engulfing"

    # Bearish engulfing
    if is_bull(-2) and is_bear(i) and c[i] < o[-2] and o[i] > c[-2]:
        return "bearish_engulfing"

    # Doji
    if candle(i) > 0 and body(i) < candle(i) * 0.1:
        return "doji"

    # Morning star (3 candles)
    if (len(df) >= 3 and is_bear(-3) and
            body(-2) < body(-3) * 0.3 and is_bull(i) and
            c[i] > (o[-3] + c[-3]) / 2):
        return "morning_star"

    # Evening star
    if (len(df) >= 3 and is_bull(-3) and
            body(-2) < body(-3) * 0.3 and is_bear(i) and
            c[i] < (o[-3] + c[-3]) / 2):
        return "evening_star"

    return "none"


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyze(symbol: str, df: pd.DataFrame) -> Optional[Signal]:
    min_candles = max(MACD_SLOW + MACD_SIGNAL_PERIOD, RSI_PERIOD, VOLUME_MA_PERIOD) + 5
    if len(df) < min_candles:
        log.debug("%s: not enough candles (%d < %d)", symbol, len(df), min_candles)
        return None

    close  = df["close"]
    volume = df["volume"]
    price  = float(close.iloc[-1])

    rsi_s = _rsi(close)
    rsi   = float(rsi_s.iloc[-1])
    if np.isnan(rsi):
        return None

    _, _, hist_s = _macd(close)
    macd_line, _, _ = _macd(close)
    macd_val  = float(macd_line.iloc[-1])
    hist_val  = float(hist_s.iloc[-1])
    if np.isnan(hist_val):
        return None

    vol_ratio = _volume_ratio(volume)
    pattern   = _detect_pattern(df)
    ema50     = _ema(close, 50)
    above_ema = price > float(ema50.iloc[-1])

    # ── LONG ────────────────────────────────────────────────────────────────
    long_reasons: list[str] = []
    if rsi < RSI_OVERSOLD:
        long_reasons.append(f"RSI oversold ({rsi:.1f})")
    if hist_val > 0:
        long_reasons.append(f"MACD histogram positivo ({hist_val:.6f})")
    if vol_ratio >= VOLUME_THRESHOLD:
        long_reasons.append(f"Volume {vol_ratio:.1f}x sopra la media")
    if pattern in ("hammer", "bullish_engulfing", "morning_star"):
        long_reasons.append(f"Pattern rialzista: {pattern}")
    if above_ema:
        long_reasons.append("Prezzo sopra EMA50")

    # ── SHORT ────────────────────────────────────────────────────────────────
    short_reasons: list[str] = []
    if rsi > RSI_OVERBOUGHT:
        short_reasons.append(f"RSI overbought ({rsi:.1f})")
    if hist_val < 0:
        short_reasons.append(f"MACD histogram negativo ({hist_val:.6f})")
    if vol_ratio >= VOLUME_THRESHOLD:
        short_reasons.append(f"Volume {vol_ratio:.1f}x sopra la media")
    if pattern in ("shooting_star", "bearish_engulfing", "evening_star"):
        short_reasons.append(f"Pattern ribassista: {pattern}")
    if not above_ema:
        short_reasons.append("Prezzo sotto EMA50")

    MIN_CONFLUENCES = 3
    if len(long_reasons) >= MIN_CONFLUENCES:
        return Signal(symbol, Direction.LONG,  price, rsi, macd_val, hist_val,
                      vol_ratio, pattern, long_reasons,  len(long_reasons))
    if len(short_reasons) >= MIN_CONFLUENCES:
        return Signal(symbol, Direction.SHORT, price, rsi, macd_val, hist_val,
                      vol_ratio, pattern, short_reasons, len(short_reasons))

    return None
