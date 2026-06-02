# Deploy su VPS (Docker Compose + HTTPS)

Architettura:
```
                    ┌──────────────────────────────┐
   Internet ──443──►│ Caddy (HTTPS auto Let's Enc.) │
                    └──────────────┬───────────────┘
                                   │ reverse proxy
                    ┌──────────────▼───────────────┐
                    │ dashboard (FastAPI + login)   │──┐
                    └───────────────────────────────┘  │  legge/scrive
                    ┌───────────────────────────────┐  │  data/trading.db
                    │ bot (scanner + Telegram)      │──┘  (SQLite WAL)
                    └───────────────────────────────┘
```
Tre container: `bot`, `dashboard`, `caddy`. Bot e dashboard condividono lo stesso file SQLite via volume `./data`.

---

## 1. Prerequisiti VPS
- Un VPS Ubuntu 22.04 (es. Hetzner, DigitalOcean, Contabo). Bastano 1 vCPU / 1 GB RAM.
- Un dominio (es. `trading.tuodominio.com`) con record **A** che punta all'IP del VPS.
- Porte **80** e **443** aperte nel firewall.

## 2. Installa Docker
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER     # poi rilogga la sessione SSH
```

## 3. Copia il progetto sul VPS
```bash
# Dal tuo PC:
scp -r E:\Etoro\trading_bot  utente@IP_VPS:~/trading_bot
# oppure clona da git se lo metti in un repo
```

## 4. Configura il .env
```bash
cd ~/trading_bot
cp .env.example .env
nano .env
```
Compila **tutti** i campi. Per la dashboard pubblica imposta in particolare:
```
DASHBOARD_DOMAIN=trading.tuodominio.com
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=<password forte>
DASHBOARD_SECRET=<stringa random lunga>     # genera con: openssl rand -hex 32
USE_TESTNET=true                            # parti SEMPRE in testnet
```

## 5. Avvia
```bash
docker compose up -d --build
```
Caddy richiede automaticamente il certificato HTTPS al primo avvio (richiede che il
dominio punti già al VPS). Dopo ~30s la dashboard è su:
```
https://trading.tuodominio.com
```
Login con le credenziali del `.env`.

## 6. Comandi utili
```bash
docker compose logs -f bot          # log del bot in tempo reale
docker compose logs -f dashboard    # log della dashboard
docker compose ps                   # stato container
docker compose restart bot          # riavvia solo il bot
docker compose down                 # ferma tutto
docker compose up -d --build        # aggiorna dopo modifiche al codice
```

## 7. Backup del database
Il file `data/trading.db` contiene tutto lo storico. Backup:
```bash
cp data/trading.db ~/backup_$(date +%F).db
```

---

## Sicurezza — checklist prima di andare in produzione reale
- [ ] `USE_TESTNET=true` finché non hai validato la strategia per giorni.
- [ ] API key Binance con **solo** "Spot Trading" abilitato, **mai** withdrawal.
- [ ] Restringi l'API key Binance all'IP del VPS (whitelist IP).
- [ ] Password dashboard forte + `DASHBOARD_SECRET` casuale.
- [ ] `ufw` attivo: `sudo ufw allow 22,80,443/tcp && sudo ufw enable`.
- [ ] Quando passi a `USE_TESTNET=false`, inizia con `TRADE_QUANTITY` minimo.

## Note operative
- La dashboard si aggiorna da sola ogni 30 secondi (polling JSON).
- Lo scanner registra un punto di equity ad ogni ciclo (`SCAN_INTERVAL_SEC`),
  così l'equity curve si popola anche senza trade (segue solo il benchmark BTC).
- Il PnL e il win rate compaiono solo dopo che un trade viene **chiuso**
  (un BUY seguito da un SELL sullo stesso simbolo, o viceversa).
