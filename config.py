import os
from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

TRADE_SYMBOL     = os.getenv("TRADE_SYMBOL", "BTCUSDC")
TRADE_QUANTITY   = float(os.getenv("TRADE_QUANTITY", "0.001"))   # fallback, non usato
TRADE_AMOUNT_USDC = float(os.getenv("TRADE_AMOUNT_USDC", "20")) # importo fisso per ordine
MAX_OPEN_TRADES  = int(os.getenv("MAX_OPEN_TRADES", "3"))
USE_TESTNET      = os.getenv("USE_TESTNET", "true").lower() == "true"

SPOT_ONLY         = os.getenv("SPOT_ONLY", "false").lower() == "true"
AUTO_TRADE        = os.getenv("AUTO_TRADE", "false").lower() == "true"
REGIME_FILTER     = os.getenv("REGIME_FILTER", "true").lower() == "true"  # LONG simulato se BTC < EMA50
USE_MARGIN        = os.getenv("USE_MARGIN", "true").lower() == "true"  # margin API per LONG e SHORT
ENABLE_SL_TP         = os.getenv("ENABLE_SL_TP", "true").lower() == "true"
STOP_LOSS_PCT        = float(os.getenv("STOP_LOSS_PCT", "2.0"))
TAKE_PROFIT_PCT      = float(os.getenv("TAKE_PROFIT_PCT", "4.0"))
ENABLE_TRAILING_STOP     = os.getenv("ENABLE_TRAILING_STOP", "false").lower() == "true"
TRAILING_STOP_PCT        = float(os.getenv("TRAILING_STOP_PCT", "1.5"))
TRAILING_ACTIVATION_PCT  = float(os.getenv("TRAILING_ACTIVATION_PCT", "1.0"))

RSI_PERIOD        = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERSOLD      = float(os.getenv("RSI_OVERSOLD", "30"))
RSI_OVERBOUGHT    = float(os.getenv("RSI_OVERBOUGHT", "70"))
MACD_FAST         = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW         = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL_PERIOD = int(os.getenv("MACD_SIGNAL", "9"))
VOLUME_MA_PERIOD  = int(os.getenv("VOLUME_MA_PERIOD", "20"))
VOLUME_THRESHOLD  = float(os.getenv("VOLUME_THRESHOLD", "1.5"))

TIMEFRAME         = "1h"
CANDLES_LIMIT     = 100
SCAN_INTERVAL_SEC = 60
MONITOR_INTERVAL_SEC          = int(os.getenv("MONITOR_INTERVAL_SEC", "15"))
REPORT_INTERVAL_HOURS         = int(os.getenv("REPORT_INTERVAL_HOURS", "8"))
POSITIONS_REPORT_INTERVAL_SEC = REPORT_INTERVAL_HOURS * 3600
SYMBOLS_TO_SCAN = ["BTCUSDC", "ETHUSDC", "BNBUSDC", "SOLUSDC", "ADAUSDC", "SUIUSDC", "DOGEUSDC"]

# ── Persistence / Dashboard ──────────────────────────────────────────────────
DB_PATH          = os.getenv("DB_PATH", "data/trading.db")
INITIAL_CAPITAL  = float(os.getenv("INITIAL_CAPITAL", "100"))
GOAL_CAPITAL     = float(os.getenv("GOAL_CAPITAL", "1000"))
EXPERIMENT_DAYS  = int(os.getenv("EXPERIMENT_DAYS", "7"))
BENCHMARK_SYMBOL = os.getenv("BENCHMARK_SYMBOL", "BTCUSDC")

DASHBOARD_USER     = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")
DASHBOARD_SECRET   = os.getenv("DASHBOARD_SECRET", "please-change-this-secret")
DASHBOARD_PORT     = int(os.getenv("DASHBOARD_PORT", "8000"))
ENABLE_OCO = os.getenv("ENABLE_OCO", "true").lower() == "true"
