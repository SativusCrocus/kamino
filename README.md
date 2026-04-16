# KAMINO YIELD REAPER

Autonomous Solana yield optimizer. Runs forever. Zero gas waste. Works with $17.

## What it does

- Every 5 minutes: polls Kamino for best risk-adjusted APY across safe assets (SOL, jitoSOL, mSOL, bSOL, USDC)
- Only migrates if the yield delta covers gas cost within 7 days
- Logs everything. Alerts via Telegram (optional). Never touches private keys.
- Compounds automatically because Kamino auto-compounds deposited assets

## What it does NOT do

- It does not execute transactions automatically (you deposit once manually, Kamino compounds for you)
- It does not hold your private key
- It does not promise 10-second profits — that math doesn't work at $17

## Setup

### 1. Get a free Helius RPC key
https://helius.dev → Sign up → Create App → Copy API key

### 2. Install
```bash
pip3 install -r requirements.txt
cp .env.example .env
```

### 3. Fill in .env
```
WALLET_ADDRESS=your_phantom_public_key   # public key only, never private
HELIUS_RPC=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
```

### 4. (Optional) Telegram alerts
- Message @BotFather on Telegram → /newbot → copy token
- Start chat with your bot
- Visit: `https://api.telegram.org/bot<TOKEN>/getUpdates` → get your chat_id
- Add both to .env

### 5. Deploy
```bash
# Quick start (any OS)
chmod +x deploy.sh
./deploy.sh

# Production (Linux VPS with systemd)
./deploy.sh --systemd
```

### 6. Monitor
```bash
# Live dashboard
python3 dashboard.py

# Raw logs
tail -f reaper.log

# If using systemd
journalctl -u kamino-reaper -f
```

### 7. Deposit once (manual, you stay in control)
The agent will log the recommended vault address on first run.
Open Kamino Finance, deposit your SOL there. Done.
Kamino auto-compounds. The agent monitors and tells you when to move.

## Architecture

```
reaper.py          — main agent loop
dashboard.py       — terminal monitor
deploy.sh          — one-command deploy (nohup or systemd)
requirements.txt   — Python dependencies
reaper_state.json  — persisted state (auto-created, gitignored)
reaper.log         — full audit log (gitignored)
.env               — config (never committed)
```

## Running on Oracle Cloud (free, forever)

Oracle Cloud free tier: 4 ARM cores, 24GB RAM, unlimited bandwidth.
1. Create free account at cloud.oracle.com
2. Launch ARM Ubuntu instance (always free tier)
3. SSH in, clone this repo, follow setup above
4. Deploy with `./deploy.sh --systemd` for auto-restart
5. Agent runs 24/7 at $0/month

## Honest expectations at $17

| Timeframe | Value at 8% APY | Value at 15% APY |
|-----------|----------------|-----------------|
| Start     | $17.00         | $17.00          |
| 30 days   | $17.11         | $17.20          |
| 1 year    | $18.36         | $19.55          |
| 3 years   | $21.46         | $26.00          |

The system is real and correct. The math is the math.
The value is in: (a) the working infrastructure, (b) showing it to people with more capital.

## Security

- Private key never enters this system
- Public key used only for read-only balance queries
- No on-chain signing — you execute migrations manually when alerted
- State file contains no sensitive data

## License

MIT
