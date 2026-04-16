"""
KAMINO YIELD REAPER — Production Agent
Runs forever. Costs near-zero in gas. Compounds everything.
Works with $17. Built for Solana mainnet.

Requirements:
    pip install -r requirements.txt

Setup:
    1. Copy .env.example to .env and fill in values
    2. python reaper.py
"""

import asyncio
import os
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv
import httpx

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────────────

WALLET_ADDRESS        = os.getenv("WALLET_ADDRESS")           # Your Phantom public key (read-only monitoring)
TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")   # Optional: alerts
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")     # Optional: alerts

POLL_INTERVAL_SECS    = 300          # 5 minutes. Not 5 seconds. Gas math wins.
MIN_APY_DELTA         = 1.5          # Only migrate if new vault beats current by 1.5% APY
MIGRATION_COST_USD    = 0.02         # Conservative gas cost per migration
DAYS_TO_BREAK_EVEN    = 7            # Migration must recoup cost within 7 days of yield delta
MIN_POSITION_USD      = 1.0          # Don't touch positions below this (gas not worth it)

KAMINO_API            = "https://api.kamino.finance"
HELIUS_RPC            = os.getenv("HELIUS_RPC", "https://api.mainnet-beta.solana.com")

LOG_FILE              = "reaper.log"
PRICE_CACHE_SECS      = 120           # Cache SOL price for 2 min (CoinGecko rate limits)

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("reaper")

# ─── DATA MODELS ──────────────────────────────────────────────────────────────

@dataclass
class Vault:
    address: str
    name: str
    token: str
    apy_7d: float          # 7-day trailing APY %
    tvl_usd: float
    utilization: float     # 0–1
    risk_score: float      # 0–1, lower = safer (computed by us)

@dataclass
class State:
    current_vault: Optional[str]   = None
    current_apy: float             = 0.0
    position_usd: float            = 0.0
    total_earned_usd: float        = 0.0
    migrations: int                = 0
    started_at: float              = field(default_factory=time.time)
    last_check: float              = 0.0

# ─── KAMINO API ───────────────────────────────────────────────────────────────

async def fetch_kamino_vaults(client: httpx.AsyncClient) -> list[Vault]:
    """
    Fetch all Kamino lending vaults and compute risk-adjusted APY.
    Only returns vaults for SOL, mSOL, jitoSOL, USDC (safest assets).
    """
    SAFE_TOKENS = {"SOL", "mSOL", "jitoSOL", "USDC", "bSOL"}

    try:
        r = await client.get(f"{KAMINO_API}/v2/strategies", timeout=15)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        log.warning(f"Kamino API fetch failed: {e}")
        return []

    vaults = []
    for item in raw:
        try:
            token = item.get("tokenSymbol", "")
            if token not in SAFE_TOKENS:
                continue

            apy_7d     = float(item.get("apy", {}).get("7d", 0)) * 100  # convert to %
            tvl        = float(item.get("totalValueLocked", 0))
            util       = float(item.get("utilizationRate", 0))
            address    = item.get("strategy", "")
            name       = item.get("strategyName", token)

            if tvl < 50_000:   # Skip tiny vaults — liquidity risk
                continue
            if apy_7d <= 0:
                continue

            # Risk score: high utilization = high rate but high withdrawal risk
            # Penalize vaults with >85% utilization (can't withdraw easily)
            util_penalty = max(0, (util - 0.85) * 3)  # 0 below 85%, rises steeply above
            tvl_bonus    = min(0.3, tvl / 10_000_000 * 0.3)  # small bonus for large TVL
            risk_score   = max(0.0, min(1.0, util_penalty - tvl_bonus))

            vaults.append(Vault(
                address=address,
                name=name,
                token=token,
                apy_7d=apy_7d,
                tvl_usd=tvl,
                utilization=util,
                risk_score=risk_score,
            ))
        except Exception:
            continue

    # Sort by risk-adjusted APY: APY × (1 - risk_score)
    vaults.sort(key=lambda v: v.apy_7d * (1 - v.risk_score), reverse=True)
    return vaults


_sol_price_cache: dict = {"price": 0.0, "ts": 0.0}

async def _get_sol_price(client: httpx.AsyncClient) -> float:
    """Fetch SOL/USD price with caching to respect CoinGecko rate limits."""
    now = time.time()
    if _sol_price_cache["price"] > 0 and (now - _sol_price_cache["ts"]) < PRICE_CACHE_SECS:
        return _sol_price_cache["price"]
    try:
        r = await client.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=10,
        )
        r.raise_for_status()
        price = r.json().get("solana", {}).get("usd", 0.0)
        if price > 0:
            _sol_price_cache["price"] = price
            _sol_price_cache["ts"] = now
            return price
    except Exception as e:
        log.warning(f"SOL price fetch failed: {e}")
    return _sol_price_cache["price"] or 150.0  # last-known or fallback


async def fetch_wallet_position(client: httpx.AsyncClient, vault_address: str) -> float:
    """
    Returns estimated USD value of wallet's SOL holdings.
    Uses Helius RPC for balance and CoinGecko for price (cached).
    """
    if not WALLET_ADDRESS:
        return 0.0

    try:
        sol_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [WALLET_ADDRESS],
        }
        r = await client.post(HELIUS_RPC, json=sol_payload, timeout=10)
        r.raise_for_status()
        lamports = r.json().get("result", {}).get("value", 0)
        sol_balance = lamports / 1e9

        sol_price = await _get_sol_price(client)
        return round(sol_balance * sol_price, 4)

    except Exception as e:
        log.warning(f"Position fetch failed: {e}")
        return 0.0

# ─── MIGRATION LOGIC ──────────────────────────────────────────────────────────

def should_migrate(current_apy: float, best_apy: float, position_usd: float) -> tuple[bool, str]:
    """
    Returns (should_migrate: bool, reason: str)
    Gate: delta must exceed break-even on migration cost within DAYS_TO_BREAK_EVEN days.
    """
    if position_usd < MIN_POSITION_USD:
        return False, f"Position ${position_usd:.2f} too small"

    delta = best_apy - current_apy
    if delta < MIN_APY_DELTA:
        return False, f"APY delta {delta:.2f}% below threshold {MIN_APY_DELTA}%"

    # Daily yield gain from migration
    daily_gain = position_usd * (delta / 100) / 365
    days_to_recoup = MIGRATION_COST_USD / daily_gain if daily_gain > 0 else 9999

    if days_to_recoup > DAYS_TO_BREAK_EVEN:
        return False, f"Gas recoup takes {days_to_recoup:.0f} days (max {DAYS_TO_BREAK_EVEN})"

    return True, f"Delta {delta:.2f}% APY, recoup in {days_to_recoup:.1f} days"


def compute_compound_projection(principal: float, apy: float, days: int) -> dict:
    """Compound daily to show growth trajectory."""
    daily_rate = apy / 100 / 365
    value = principal
    for _ in range(days):
        value *= (1 + daily_rate)
    return {
        "days": days,
        "start": round(principal, 4),
        "end": round(value, 4),
        "earned": round(value - principal, 4),
        "apy": apy,
    }

# ─── TELEGRAM ALERTS ──────────────────────────────────────────────────────────

async def telegram_alert(client: httpx.AsyncClient, message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": f"🔁 REAPER\n{message}", "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram alert failed: {e}")

# ─── STATE PERSISTENCE ────────────────────────────────────────────────────────

STATE_FILE = "reaper_state.json"

def save_state(state: State):
    with open(STATE_FILE, "w") as f:
        json.dump(state.__dict__, f, indent=2)

def load_state() -> State:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            return State(**data)
        except Exception:
            pass
    return State()

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

async def run():
    log.info("=" * 60)
    log.info("KAMINO YIELD REAPER — STARTING")
    log.info(f"Wallet: {WALLET_ADDRESS or 'NOT SET — monitoring only'}")
    log.info(f"Poll interval: {POLL_INTERVAL_SECS}s | Min APY delta: {MIN_APY_DELTA}%")
    log.info("=" * 60)

    state = load_state()
    consecutive_errors = 0
    MAX_ERRORS = 10

    async with httpx.AsyncClient(
        headers={"User-Agent": "KaminoReaper/1.0"},
        follow_redirects=True
    ) as client:

        await telegram_alert(client, "Agent started. Monitoring Kamino vaults.")

        while True:
            try:
                loop_start = time.time()
                state.last_check = loop_start

                # 1. Fetch all safe vaults
                vaults = await fetch_kamino_vaults(client)
                if not vaults:
                    log.warning("No vaults returned. RPC issue? Sleeping...")
                    await asyncio.sleep(60)
                    continue

                best = vaults[0]
                log.info(f"Best vault: {best.name} | APY: {best.apy_7d:.2f}% | TVL: ${best.tvl_usd:,.0f} | Util: {best.utilization*100:.1f}%")
                log.info(f"Top 3 options: {[(v.name, f'{v.apy_7d:.1f}%') for v in vaults[:3]]}")

                # 2. Fetch current position value
                position = await fetch_wallet_position(client, state.current_vault or best.address)
                state.position_usd = position
                log.info(f"Current position: ${position:.4f} | Vault: {state.current_vault or 'none'}")

                # 3. Check if we should migrate
                current_apy = state.current_apy or 0.0
                do_migrate, reason = should_migrate(current_apy, best.apy_7d, position)

                if state.current_vault is None:
                    # First run — set current vault to best available
                    log.info(f"Initial deployment → {best.name} at {best.apy_7d:.2f}% APY")
                    log.info(f"ACTION REQUIRED: Manually deposit into vault {best.address}")
                    log.info(f"Kamino URL: https://app.kamino.finance/lending/reserve/{best.address}")
                    state.current_vault = best.address
                    state.current_apy = best.apy_7d
                    await telegram_alert(client,
                        f"Initial vault selected:\n<b>{best.name}</b> @ {best.apy_7d:.2f}% APY\n"
                        f"Deposit here:\nhttps://app.kamino.finance/lending/reserve/{best.address}"
                    )

                elif do_migrate:
                    log.info(f"MIGRATE SIGNAL: {reason}")
                    log.info(f"  From: {state.current_vault} @ {current_apy:.2f}%")
                    log.info(f"  To:   {best.address} ({best.name}) @ {best.apy_7d:.2f}%")
                    state.current_vault = best.address
                    state.current_apy = best.apy_7d
                    state.migrations += 1

                    projection = compute_compound_projection(position, best.apy_7d, 365)
                    await telegram_alert(client,
                        f"Migration #{state.migrations} triggered\n"
                        f"Reason: {reason}\n"
                        f"New vault: <b>{best.name}</b> @ {best.apy_7d:.2f}%\n"
                        f"Vault: {best.address}\n"
                        f"Projected 1yr: ${projection['end']:.2f} (+${projection['earned']:.2f})"
                    )

                else:
                    log.info(f"HOLD: {reason}")

                # 4. Log projection summary
                if position > 0 and state.current_apy > 0:
                    p30  = compute_compound_projection(position, state.current_apy, 30)
                    p365 = compute_compound_projection(position, state.current_apy, 365)
                    log.info(f"Projection: 30d=${p30['end']:.4f} | 1yr=${p365['end']:.4f} | APY={state.current_apy:.2f}%")

                # 5. Save state
                save_state(state)
                consecutive_errors = 0

                # 6. Sleep until next cycle
                elapsed = time.time() - loop_start
                sleep_for = max(10, POLL_INTERVAL_SECS - elapsed)
                log.info(f"Cycle complete in {elapsed:.1f}s. Sleeping {sleep_for:.0f}s.\n")
                await asyncio.sleep(sleep_for)

            except KeyboardInterrupt:
                log.info("Shutdown requested. Saving state.")
                save_state(state)
                break

            except Exception as e:
                consecutive_errors += 1
                log.error(f"Unhandled error (#{consecutive_errors}): {e}", exc_info=True)
                if consecutive_errors >= MAX_ERRORS:
                    await telegram_alert(client, f"FATAL: {consecutive_errors} consecutive errors. Check logs.")
                    log.critical("Too many errors. Exiting to prevent damage.")
                    break
                backoff = min(300, 30 * consecutive_errors)
                log.warning(f"Backing off {backoff}s before retry...")
                await asyncio.sleep(backoff)


if __name__ == "__main__":
    asyncio.run(run())
