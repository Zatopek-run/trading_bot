# Trading Bot — Contesto per Claude Code

## Descrizione
Bot di trading automatico su Binance Cross Margin 3x.
Linguaggio: Python 3.11, Docker, aiosqlite, aiohttp, python-telegram-bot.

## Architettura
- main.py — entry point, scanner loop, monitor loop, report loop
- strategy.py — indicatori tecnici (RSI, MACD, EMA50, volume, candlestick)
- trader.py — esecuzione ordini su Binance REST API
- telegram_bot.py — notifiche e comandi Telegram
- database.py — SQLite async (aiosqlite)
- config.py — variabili da .env
- dashboard.py — FastAPI web dashboard

## Parametri attuali
- TRADE_AMOUNT_USDC=160, MAX_OPEN_TRADES=3
- SL 2%, TP 4%, trailing stop 1.5% con attivazione 1%
- 7 simboli: BTCUSDC, ETHUSDC, BNBUSDC, SOLUSDC, ADAUSDC, SUIUSDC, DOGEUSDC
- Timeframe: 1h, scan ogni 60s, monitor SL/TP ogni 15s
- AUTO_TRADE=true, REGIME_FILTER=true, ENABLE_OCO=false

## Regole importanti
- Non toccare mai .env (contiene credenziali reali)
- Usare sempre python3, mai python
- Il database è data/trading.db (SQLite WAL)
- trades.status può essere: open, closed, simulated
- Il trailing stop NON si attiva prima di TRAILING_ACTIVATION_PCT (1%)
- I LONG in regime BEARISH (BTC sotto EMA50) vengono simulati, non eseguiti

## Deploy
Dopo ogni modifica:
docker compose down
docker compose build --no-cache
docker compose up -d

## Errori noti
- Binance -3045: pool SOL esaurito, temporaneo, ignorare
- httpx logs disabilitati (logging.WARNING) per nascondere token Telegram
