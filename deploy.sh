#!/bin/bash
# KAMINO REAPER — One-command deploy
# Run: chmod +x deploy.sh && ./deploy.sh
# Options:
#   ./deploy.sh           — run with nohup (any OS)
#   ./deploy.sh --systemd — install as systemd service (Linux VPS)

set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " KAMINO YIELD REAPER — DEPLOY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check Python
python3 --version || { echo "Python3 required"; exit 1; }

# Install deps
echo "Installing dependencies..."
pip3 install -r requirements.txt -q

# Check .env
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  .env created. Fill in your WALLET_ADDRESS and HELIUS_RPC key."
    echo "    Edit .env then run: ./deploy.sh again"
    exit 0
fi

# Validate wallet is set
WALLET_VAL=$(grep -E '^WALLET_ADDRESS=' .env | cut -d= -f2-)
if [ -z "$WALLET_VAL" ] || [ "$WALLET_VAL" = "your_phantom_public_key_here" ]; then
    echo "❌ Set WALLET_ADDRESS in .env first"
    exit 1
fi

RPC_VAL=$(grep -E '^HELIUS_RPC=' .env | cut -d= -f2-)
echo "✓ Wallet: $WALLET_VAL"
echo "✓ RPC: $RPC_VAL"
echo ""

# ─── SYSTEMD MODE ────────────────────────────────────────────────────────────

if [ "$1" = "--systemd" ]; then
    if ! command -v systemctl &> /dev/null; then
        echo "❌ systemd not available on this system. Use ./deploy.sh without flags."
        exit 1
    fi

    WORK_DIR="$(cd "$(dirname "$0")" && pwd)"
    SERVICE_FILE="/etc/systemd/system/kamino-reaper.service"

    echo "Creating systemd service..."
    sudo tee "$SERVICE_FILE" > /dev/null <<UNIT
[Unit]
Description=Kamino Yield Reaper
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$WORK_DIR
ExecStart=$(which python3) $WORK_DIR/reaper.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

    sudo systemctl daemon-reload
    sudo systemctl enable kamino-reaper
    sudo systemctl restart kamino-reaper

    echo ""
    echo "✓ Service installed and started"
    echo "✓ Logs:   journalctl -u kamino-reaper -f"
    echo "✓ Status: systemctl status kamino-reaper"
    echo "✓ Stop:   sudo systemctl stop kamino-reaper"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo " Agent running as system service."
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 0
fi

# ─── NOHUP MODE (default) ────────────────────────────────────────────────────

echo "Starting agent..."
nohup python3 reaper.py > /dev/null 2>&1 &
AGENT_PID=$!

echo "✓ Agent PID: $AGENT_PID"
echo "✓ Logs: tail -f reaper.log"
echo ""
echo "To stop: kill $AGENT_PID"
echo "To monitor: python3 dashboard.py"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Agent is running. Close this terminal safely."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
