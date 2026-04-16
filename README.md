# KAMINO YIELD REAPER

Autonomous Solana yield optimizer. Runs forever. Zero gas waste. Works with $17.

## Two modes

| Mode | What it does | Private key needed? |
|------|-------------|-------------------|
| **Monitor** (default) | Watches vaults, sends Telegram alerts when to migrate. You move funds manually. | No |
| **Auto-Execute** | Signs and submits transactions autonomously. Deposits, withdraws, and migrates for you. | Yes |

## What it does

- Every 5 minutes: polls Kamino for best risk-adjusted APY across safe assets (SOL, jitoSOL, mSOL, bSOL, USDC)
- Only migrates if the yield delta covers gas cost within 7 days
- Logs everything. Alerts via Telegram. Compounds automatically.
- In auto mode: executes deposits and vault migrations on-chain

## Setup

### 1. Get a free Helius RPC key
https://helius.dev → Sign up → Create App → Copy API key

### 2. Install
```bash
pip3 install -r requirements.txt
cp .env.example .env
```

### 3. Fill in .env
```bash
# Required
WALLET_ADDRESS=your_phantom_public_key
HELIUS_RPC=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY

# For autonomous mode (optional — skip for monitor-only)
WALLET_PRIVATE_KEY=your_base58_private_key
AUTO_EXECUTE=true
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

### 7. Deposit (monitor mode only)
If running in monitor mode, the agent will log the recommended vault address on first run.
Open Kamino Finance, deposit your SOL there. The agent tells you when to move.

In auto-execute mode, the bot handles the initial deposit for you.

## Architecture

```
reaper.py          — main agent loop + transaction engine
dashboard.py       — terminal monitor (ANSI color UI)
deploy.sh          — one-command deploy (nohup or systemd)
requirements.txt   — Python dependencies
reaper_state.json  — persisted state (auto-created, gitignored)
reaper.log         — full audit log (gitignored)
.env               — config (never committed)
```

## How auto-execute works

1. Bot detects a better vault (APY delta > 1.5%, recoups gas in < 7 days)
2. Withdraws from current Kamino vault (signed on-device)
3. Waits for confirmation
4. Deposits into new vault (keeps 0.01 SOL reserve for gas)
5. Sends Telegram confirmation with tx details and projections

Safety guards:
- 0.01 SOL always kept as gas reserve
- 1% max slippage on all transactions
- 10 consecutive error limit before shutdown
- Exponential backoff on failures
- All transactions logged with signatures

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

- In monitor mode: private key never enters the system
- In auto mode: private key stays in `.env` (gitignored), loaded once into memory
- No remote key transmission — all signing happens locally
- Public key used for read-only balance queries
- State file contains no sensitive data
- `.env` is gitignored — never committed

## License

MIT
