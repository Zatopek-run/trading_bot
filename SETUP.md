# Setup Trading Bot

## 1. Installa dipendenze
```
pip install -r requirements.txt
```

## 2. Crea il file .env
```
copy .env.example .env
```
Poi apri `.env` e inserisci:

### Binance API
1. Vai su https://www.binance.com/it/my/settings/api-management
2. Crea una nuova API key
3. Abilita solo **Spot Trading** (NON abilitare withdrawal)
4. Per testare prima, lascia `USE_TESTNET=true` e registrati su https://testnet.binance.vision

### Telegram Bot
1. Apri Telegram e cerca **@BotFather**
2. Scrivi `/newbot` e segui le istruzioni
3. Copia il token in `TELEGRAM_BOT_TOKEN`
4. Per trovare il tuo Chat ID: cerca **@userinfobot** su Telegram e scrivici `/start`
5. Copia l'id numerico in `TELEGRAM_CHAT_ID`

## 3. Avvia il bot
```
python main.py
```

## Flusso di funzionamento
```
Scanner (ogni 60s)
  └─ fetch OHLCV da Binance (pubblico, no auth)
      └─ strategy.analyze()
          ├─ RSI < 30 (oversold) o > 70 (overbought)
          ├─ MACD histogram positivo/negativo
          ├─ Volume > 1.5x media 20 periodi
          ├─ Pattern candlestick (hammer, engulfing…)
          └─ EMA50 trend filter
              └─ se ≥ 3 confluenze → Signal
                  └─ Telegram: messaggio con bottoni [✅ Esegui] [❌ Ignora]
                      ├─ Esegui → place_order() su Binance (signed request)
                      └─ Ignora → segnale scartato
```

## Comandi Telegram
| Comando | Descrizione |
|---------|-------------|
| /start  | Messaggio di benvenuto |
| /balance | Mostra saldo USDT e BTC |
| /status | Quanti segnali in attesa |

## Parametri chiave in .env
| Parametro | Default | Significato |
|-----------|---------|-------------|
| TRADE_QUANTITY | 0.001 | BTC per ordine |
| USE_TESTNET | true | true = paper trading |
| RSI_OVERSOLD | 30 | Soglia RSI long |
| RSI_OVERBOUGHT | 70 | Soglia RSI short |
| VOLUME_THRESHOLD | 1.5 | Volume minimo (x media) |
| SCAN_INTERVAL_SEC | 60 | Secondi tra una scansione e la prossima |
| ENABLE_SL_TP | true | Attiva stop-loss/take-profit automatici |
| STOP_LOSS_PCT | 2.0 | Chiusura automatica a -2% dall'entry |
| TAKE_PROFIT_PCT | 4.0 | Chiusura automatica a +4% dall'entry |
| MONITOR_INTERVAL_SEC | 15 | Frequenza controllo prezzi per SL/TP |

## Stop-Loss / Take-Profit automatici
Quando apri una posizione con *Esegui*, il bot calcola e salva i livelli SL/TP
in base all'entry. Un loop dedicato (`MONITOR_INTERVAL_SEC`, default 15s) controlla
il prezzo di mercato di ogni posizione aperta e, se tocca lo stop o il target,
piazza automaticamente l'ordine di chiusura e ti notifica su Telegram con il PnL.
Il trade chiuso compare subito nella dashboard con `close_reason` = stop_loss / take_profit.
