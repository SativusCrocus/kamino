"""
KAMINO YIELD REAPER — Autonomous Production Agent
Runs forever. Costs near-zero in gas. Compounds everything.
Works with $17. Built for Solana mainnet.

Modes:
    MONITOR ONLY (default):  Watches vaults, alerts via Telegram. You migrate manually.
    AUTO-EXECUTE:            Signs and submits transactions. Fully autonomous.

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
import base64
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv
import httpx
import base58

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────────────

WALLET_ADDRESS        = os.getenv("WALLET_ADDRESS")
WALLET_PRIVATE_KEY    = os.getenv("WALLET_PRIVATE_KEY", "")
AUTO_EXECUTE          = os.getenv("AUTO_EXECUTE", "false").lower() == "true"
TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")

POLL_INTERVAL_SECS    = 300          # 5 minutes
MIN_APY_DELTA         = 1.5          # Only migrate if delta > 1.5% APY
MIGRATION_COST_USD    = 0.02         # Conservative gas cost per migration
DAYS_TO_BREAK_EVEN    = 7            # Must recoup gas within 7 days
MIN_POSITION_USD      = 1.0          # Don't touch tiny positions
MAX_SLIPPAGE_BPS      = 100          # 1% max slippage for swaps
SOL_RESERVE_LAMPORTS  = 50_000_000   # Keep 0.05 SOL for gas + account creation rent

KAMINO_API            = "https://api.kamino.finance"
KAMINO_KTX            = "https://api.kamino.finance/ktx"
HELIUS_RPC            = os.getenv("HELIUS_RPC", "https://api.mainnet-beta.solana.com")

# Kamino lending markets to scan (mainnet)
KAMINO_MARKETS = [
    "DxXdAyU3kCjnyggvHmY5nAwg5cRbbmdyX3npfDMjjMek",   # JLP Market
    "H6rHXmXoCQvq8Ue81MqNh7ow5ysPa1dSozwW3PU1dDH6",   # Main Market (SOL lending)
    "ByYiZxp8QrdN9qbdtaAiePN8AAr3qvTPppNJDpf5DVJ5",   # Altcoin Market
]

LOG_FILE              = "reaper.log"
PRICE_CACHE_SECS      = 120

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
    apy_7d: float
    tvl_usd: float
    utilization: float
    risk_score: float
    vault_type: str = "strategy"  # "strategy" (kvault) or "reserve" (klend)
    market: str = ""              # klend market address (for reserves)

@dataclass
class State:
    current_vault: Optional[str]   = None
    current_vault_type: str        = ""    # "strategy" or "reserve"
    current_vault_market: str      = ""    # klend market (if reserve)
    current_vault_name: str        = ""
    current_apy: float             = 0.0
    position_usd: float            = 0.0
    total_earned_usd: float        = 0.0
    migrations: int                = 0
    started_at: float              = field(default_factory=time.time)
    last_check: float              = 0.0
    mode: str                      = "monitor"

# ─── WALLET / KEYPAIR ────────────────────────────────────────────────────────

_keypair = None

def get_keypair():
    """Load wallet keypair from private key. Cached after first call."""
    global _keypair
    if _keypair is not None:
        return _keypair
    if not WALLET_PRIVATE_KEY:
        return None
    try:
        from solders.keypair import Keypair
        secret = base58.b58decode(WALLET_PRIVATE_KEY)
        _keypair = Keypair.from_bytes(secret)
        log.info(f"Keypair loaded: {_keypair.pubkey()}")
        return _keypair
    except Exception as e:
        log.error(f"Failed to load keypair: {e}")
        return None

# ─── SOLANA RPC HELPERS ──────────────────────────────────────────────────────

async def rpc_call(client: httpx.AsyncClient, method: str, params: list) -> dict:
    """Make a JSON-RPC call to Solana."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = await client.post(HELIUS_RPC, json=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise Exception(f"RPC error: {data['error']}")
    return data.get("result", {})


async def get_sol_balance(client: httpx.AsyncClient) -> int:
    """Get wallet SOL balance in lamports."""
    if not WALLET_ADDRESS:
        return 0
    result = await rpc_call(client, "getBalance", [WALLET_ADDRESS])
    return result.get("value", 0)


async def get_token_accounts(client: httpx.AsyncClient) -> list[dict]:
    """Get all SPL token accounts for the wallet."""
    if not WALLET_ADDRESS:
        return []
    result = await rpc_call(client, "getTokenAccountsByOwner", [
        WALLET_ADDRESS,
        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
        {"encoding": "jsonParsed"}
    ])
    return result.get("value", [])


async def get_recent_blockhash(client: httpx.AsyncClient) -> str:
    """Get a recent blockhash for transaction construction."""
    result = await rpc_call(client, "getLatestBlockhash", [{"commitment": "finalized"}])
    return result.get("value", {}).get("blockhash", "")


async def send_transaction(client: httpx.AsyncClient, signed_tx_bytes: bytes) -> str:
    """Submit a signed transaction and return the signature."""
    encoded = base64.b64encode(signed_tx_bytes).decode("utf-8")
    result = await rpc_call(client, "sendTransaction", [
        encoded,
        {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}
    ])
    return result


async def confirm_transaction(client: httpx.AsyncClient, signature: str, timeout: int = 60) -> bool:
    """Wait for transaction confirmation."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            result = await rpc_call(client, "getSignatureStatuses", [[signature]])
            statuses = result.get("value", [])
            if statuses and statuses[0]:
                status = statuses[0]
                if status.get("err"):
                    log.error(f"Transaction failed: {status['err']}")
                    return False
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
        except Exception:
            pass
        await asyncio.sleep(2)
    log.warning(f"Transaction confirmation timeout after {timeout}s")
    return False

# ─── KAMINO TRANSACTION BUILDER ──────────────────────────────────────────────

async def _sign_kamino_tx(client: httpx.AsyncClient, raw_tx: bytes) -> Optional[bytes]:
    """Deserialize a Kamino transaction and sign it."""
    kp = get_keypair()
    if not kp:
        return None
    try:
        from solders.transaction import VersionedTransaction

        # The KTX API returns a transaction with a recent blockhash already set.
        # We just need to sign it with our keypair.
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = VersionedTransaction(tx.message, [kp])
        return bytes(signed_tx)
    except Exception as e:
        log.error(f"Failed to sign tx: {e}")
        return None


async def build_kamino_deposit_tx(
    client: httpx.AsyncClient,
    vault: "Vault",
    amount: str,
) -> Optional[bytes]:
    """
    Build a Kamino deposit transaction via the /ktx/ API.
    Handles both strategy (kvault) and reserve (klend) deposits.
    """
    kp = get_keypair()
    if not kp:
        return None

    try:
        if vault.vault_type == "reserve":
            # klend deposit
            r = await client.post(
                f"{KAMINO_KTX}/klend/deposit",
                json={
                    "wallet": str(kp.pubkey()),
                    "market": vault.market,
                    "reserve": vault.address,
                    "amount": amount,
                },
                timeout=20,
            )
        else:
            # kvault deposit
            r = await client.post(
                f"{KAMINO_KTX}/kvault/deposit",
                json={
                    "wallet": str(kp.pubkey()),
                    "kvault": vault.address,
                    "amount": amount,
                },
                timeout=20,
            )

        r.raise_for_status()
        tx_data = r.json()
        raw_tx = base64.b64decode(tx_data.get("transaction", ""))
        if not raw_tx:
            log.warning("Kamino API returned empty deposit transaction")
            return None
        return await _sign_kamino_tx(client, raw_tx)

    except httpx.HTTPStatusError as e:
        log.warning(f"Kamino deposit API error ({e.response.status_code}): {e.response.text[:200]}")
        return None
    except Exception as e:
        log.error(f"Failed to build deposit tx: {e}")
        return None


async def build_kamino_withdraw_tx(
    client: httpx.AsyncClient,
    vault: "Vault",
    amount: str,
) -> Optional[bytes]:
    """
    Build a Kamino withdraw transaction via the /ktx/ API.
    Use amount="18446744073709551615" (U64::MAX) to withdraw all.
    """
    kp = get_keypair()
    if not kp:
        return None

    try:
        if vault.vault_type == "reserve":
            r = await client.post(
                f"{KAMINO_KTX}/klend/withdraw",
                json={
                    "wallet": str(kp.pubkey()),
                    "market": vault.market,
                    "reserve": vault.address,
                    "amount": amount,
                },
                timeout=20,
            )
        else:
            r = await client.post(
                f"{KAMINO_KTX}/kvault/withdraw",
                json={
                    "wallet": str(kp.pubkey()),
                    "kvault": vault.address,
                    "amount": amount,
                },
                timeout=20,
            )

        r.raise_for_status()
        tx_data = r.json()
        raw_tx = base64.b64decode(tx_data.get("transaction", ""))
        if not raw_tx:
            log.warning("Kamino API returned empty withdraw transaction")
            return None
        return await _sign_kamino_tx(client, raw_tx)

    except httpx.HTTPStatusError as e:
        log.warning(f"Kamino withdraw API error ({e.response.status_code}): {e.response.text[:200]}")
        return None
    except Exception as e:
        log.error(f"Failed to build withdraw tx: {e}")
        return None

# ─── AUTONOMOUS MIGRATION ───────────────────────────────────────────────────

async def execute_migration(
    client: httpx.AsyncClient,
    from_vault: Vault,
    to_vault: Vault,
) -> bool:
    """
    Execute a full vault migration: withdraw from old → deposit into new.
    Returns True if successful.
    """
    log.info(f"AUTO-EXECUTE: Migrating {from_vault.name} → {to_vault.name}")

    # Step 1: Withdraw all from current vault (U64::MAX = withdraw everything)
    log.info("Step 1/2: Withdrawing from current vault...")
    withdraw_tx = await build_kamino_withdraw_tx(client, from_vault, "18446744073709551615")
    if not withdraw_tx:
        log.error("Failed to build withdraw transaction. Aborting migration.")
        return False

    try:
        withdraw_sig = await send_transaction(client, withdraw_tx)
    except Exception as e:
        log.error(f"Withdraw transaction failed: {e}")
        return False
    if not withdraw_sig:
        log.error("Failed to submit withdraw transaction.")
        return False

    log.info(f"Withdraw TX submitted: {withdraw_sig}")
    confirmed = await confirm_transaction(client, withdraw_sig)
    if not confirmed:
        log.error("Withdraw transaction not confirmed. Check manually!")
        return False

    log.info("Withdraw confirmed. Waiting 3s for balance to settle...")
    await asyncio.sleep(3)

    # Step 2: Get updated balance and deposit into new vault
    log.info("Step 2/2: Depositing into new vault...")
    balance = await get_sol_balance(client)
    deposit_amount = max(0, balance - SOL_RESERVE_LAMPORTS)

    if deposit_amount <= 0:
        log.warning("No balance available after withdrawal. Migration incomplete.")
        return False

    deposit_tx = await build_kamino_deposit_tx(client, to_vault, f"{deposit_amount / 1e9:.9f}")
    if not deposit_tx:
        log.error("Failed to build deposit transaction. Funds are in wallet — not lost.")
        return False

    try:
        deposit_sig = await send_transaction(client, deposit_tx)
    except Exception as e:
        log.error(f"Deposit transaction failed: {e}. Funds are in wallet.")
        return False
    if not deposit_sig:
        log.error("Failed to submit deposit transaction. Funds are in wallet.")
        return False

    log.info(f"Deposit TX submitted: {deposit_sig}")
    confirmed = await confirm_transaction(client, deposit_sig)
    if not confirmed:
        log.error("Deposit transaction not confirmed. Funds may be in wallet. Check manually!")
        return False

    log.info("Migration complete! Funds deposited into new vault.")
    return True


async def execute_initial_deposit(client: httpx.AsyncClient, vault: Vault) -> bool:
    """Execute the first deposit into a Kamino vault."""
    log.info(f"AUTO-EXECUTE: Initial deposit into {vault.name} ({vault.address[:16]}...)")

    balance = await get_sol_balance(client)
    deposit_amount = max(0, balance - SOL_RESERVE_LAMPORTS)

    if deposit_amount <= 0:
        log.warning(f"Balance too low for deposit ({balance} lamports). Need > {SOL_RESERVE_LAMPORTS}")
        return False

    sol_amount = deposit_amount / 1e9
    log.info(f"Depositing {sol_amount:.6f} SOL (keeping {SOL_RESERVE_LAMPORTS / 1e9} SOL for gas)")

    # Kamino KTX expects amount as decimal string (in token units, not lamports)
    deposit_tx = await build_kamino_deposit_tx(client, vault, f"{sol_amount:.9f}")
    if not deposit_tx:
        log.error("Failed to build deposit transaction.")
        return False

    try:
        sig = await send_transaction(client, deposit_tx)
    except Exception as e:
        log.error(f"Deposit transaction failed: {e}")
        return False
    if not sig:
        log.error("Failed to submit deposit transaction.")
        return False

    log.info(f"Deposit TX submitted: {sig}")
    confirmed = await confirm_transaction(client, sig)
    if not confirmed:
        log.error("Deposit transaction not confirmed. Check manually!")
        return False

    log.info("Initial deposit confirmed!")
    return True

# ─── KAMINO API ───────────────────────────────────────────────────────────────

async def fetch_kamino_vaults(client: httpx.AsyncClient) -> list[Vault]:
    """
    Fetch Kamino strategies (liquidity vaults) and lending reserves.
    Only returns vaults for safe assets with meaningful TVL.
    """
    # Assets to track (SOL-related for strategies, stables for lending)
    SAFE_TOKENS = {"SOL", "MSOL", "JITOSOL", "BSOL", "USDC", "USDT"}
    vaults = []

    # ─── Source 1: Liquidity strategies (kvaults) ─────────────────────────
    try:
        r = await client.get(
            f"{KAMINO_API}/strategies/metrics",
            params={"env": "mainnet-beta", "status": "LIVE"},
            timeout=20,
        )
        r.raise_for_status()
        raw = r.json()

        for item in raw:
            try:
                token_a = (item.get("tokenA") or "").upper()
                token_b = (item.get("tokenB") or "").upper()
                has_safe = token_a in SAFE_TOKENS or token_b in SAFE_TOKENS
                if not has_safe:
                    continue

                tvl = float(item.get("totalValueLocked") or 0)
                if tvl < 50_000:
                    continue

                # Get best available APY (prefer 7d, fall back to 24h)
                kamino_apy = item.get("kaminoApy", {}).get("vault", {})
                apy_7d = float(kamino_apy.get("apy7d") or kamino_apy.get("apy24d") or 0)
                if apy_7d <= 0:
                    apy_vault = item.get("apy", {}).get("vault", {})
                    apy_7d = float(apy_vault.get("totalApy") or 0)
                if apy_7d <= 0:
                    continue

                apy_pct = apy_7d * 100 if apy_7d < 1 else apy_7d  # normalize to %

                address = item.get("strategy", "")
                token_label = f"{item.get('tokenA','')}-{item.get('tokenB','')}"
                name = f"{token_label} vault"

                # Risk: penalize very high APY (likely volatile)
                risk_score = min(1.0, max(0.0, (apy_pct - 30) / 100)) if apy_pct > 30 else 0.0
                tvl_bonus = min(0.2, tvl / 10_000_000 * 0.2)
                risk_score = max(0.0, risk_score - tvl_bonus)

                vaults.append(Vault(
                    address=address, name=name, token=token_label,
                    apy_7d=apy_pct, tvl_usd=tvl, utilization=0.0,
                    risk_score=risk_score, vault_type="strategy",
                ))
            except Exception:
                continue
    except Exception as e:
        log.warning(f"Kamino strategies fetch failed: {e}")

    # ─── Source 2: Lending reserves (klend) — scan all markets ──────────
    for market_addr in KAMINO_MARKETS:
        try:
            r = await client.get(
                f"{KAMINO_API}/kamino-market/{market_addr}/reserves/metrics",
                timeout=20,
            )
            r.raise_for_status()
            raw = r.json()

            for item in raw:
                try:
                    token = (item.get("liquidityToken") or "").upper()
                    if token not in SAFE_TOKENS:
                        continue

                    supply_apy = float(item.get("supplyApy") or 0)
                    if supply_apy <= 0:
                        continue

                    apy_pct = supply_apy * 100 if supply_apy < 1 else supply_apy
                    tvl = float(item.get("totalSupplyUsd") or 0)
                    if tvl < 10_000:
                        continue

                    total_borrow = float(item.get("totalBorrowUsd") or item.get("totalBorrow") or 0)
                    util = total_borrow / tvl if tvl > 0 else 0

                    address = item.get("reserve", "")
                    name = f"{item.get('liquidityToken', token)} lending"

                    util_penalty = max(0, (util - 0.85) * 3)
                    tvl_bonus = min(0.3, tvl / 10_000_000 * 0.3)
                    risk_score = max(0.0, min(1.0, util_penalty - tvl_bonus))

                    vaults.append(Vault(
                        address=address, name=name, token=item.get("liquidityToken", token),
                        apy_7d=apy_pct, tvl_usd=tvl, utilization=util,
                        risk_score=risk_score, vault_type="reserve",
                        market=market_addr,
                    ))
                except Exception:
                    continue
        except Exception as e:
            log.warning(f"Kamino market {market_addr[:12]}... fetch failed: {e}")

    # Sort by risk-adjusted APY
    vaults.sort(key=lambda v: v.apy_7d * (1 - v.risk_score), reverse=True)
    log.info(f"Found {len(vaults)} vaults ({sum(1 for v in vaults if v.vault_type == 'strategy')} strategies, {sum(1 for v in vaults if v.vault_type == 'reserve')} reserves)")
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
    return _sol_price_cache["price"] or 150.0


async def fetch_wallet_position(client: httpx.AsyncClient, vault_address: str) -> float:
    """Returns estimated USD value of wallet's SOL holdings."""
    if not WALLET_ADDRESS:
        return 0.0
    try:
        lamports = await get_sol_balance(client)
        sol_balance = lamports / 1e9
        sol_price = await _get_sol_price(client)
        return round(sol_balance * sol_price, 4)
    except Exception as e:
        log.warning(f"Position fetch failed: {e}")
        return 0.0

# ─── MIGRATION LOGIC ────────────────────────────────────────────────────────

def should_migrate(current_apy: float, best_apy: float, position_usd: float) -> tuple[bool, str]:
    if position_usd < MIN_POSITION_USD:
        return False, f"Position ${position_usd:.2f} too small"

    delta = best_apy - current_apy
    if delta < MIN_APY_DELTA:
        return False, f"APY delta {delta:.2f}% below threshold {MIN_APY_DELTA}%"

    daily_gain = position_usd * (delta / 100) / 365
    days_to_recoup = MIGRATION_COST_USD / daily_gain if daily_gain > 0 else 9999

    if days_to_recoup > DAYS_TO_BREAK_EVEN:
        return False, f"Gas recoup takes {days_to_recoup:.0f} days (max {DAYS_TO_BREAK_EVEN})"

    return True, f"Delta {delta:.2f}% APY, recoup in {days_to_recoup:.1f} days"


def compute_compound_projection(principal: float, apy: float, days: int) -> dict:
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

# ─── TELEGRAM ALERTS ────────────────────────────────────────────────────────

async def telegram_alert(client: httpx.AsyncClient, message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        chat_id = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID.isdigit() else TELEGRAM_CHAT_ID
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": f"🔁 REAPER\n{message}", "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram alert failed: {e}")

# ─── STATE PERSISTENCE ──────────────────────────────────────────────────────

STATE_FILE = "reaper_state.json"

def save_state(state: State):
    with open(STATE_FILE, "w") as f:
        json.dump(state.__dict__, f, indent=2)

def load_state() -> State:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            # Only pass fields that State expects
            valid = {f.name for f in State.__dataclass_fields__.values()}
            filtered = {k: v for k, v in data.items() if k in valid}
            return State(**filtered)
        except Exception:
            pass
    return State()

# ─── MAIN LOOP ───────────────────────────────────────────────────────────────

async def run():
    can_execute = AUTO_EXECUTE and bool(WALLET_PRIVATE_KEY)
    mode = "AUTO-EXECUTE" if can_execute else "MONITOR-ONLY"

    log.info("=" * 60)
    log.info("KAMINO YIELD REAPER — STARTING")
    log.info(f"Mode: {mode}")
    log.info(f"Wallet: {WALLET_ADDRESS or 'NOT SET'}")
    if can_execute:
        kp = get_keypair()
        if not kp:
            log.error("AUTO_EXECUTE enabled but keypair failed to load. Falling back to monitor mode.")
            can_execute = False
            mode = "MONITOR-ONLY"
        else:
            log.info(f"Keypair OK: {kp.pubkey()}")
            log.info(f"Gas reserve: {SOL_RESERVE_LAMPORTS / 1e9} SOL | Max slippage: {MAX_SLIPPAGE_BPS}bps")
    log.info(f"Poll: {POLL_INTERVAL_SECS}s | Min APY delta: {MIN_APY_DELTA}%")
    log.info("=" * 60)

    state = load_state()
    state.mode = mode.lower().replace("-", "_")
    consecutive_errors = 0
    MAX_ERRORS = 10

    async with httpx.AsyncClient(
        headers={"User-Agent": "KaminoReaper/2.0"},
        follow_redirects=True
    ) as client:

        await telegram_alert(client, f"Agent started in <b>{mode}</b> mode.\nMonitoring Kamino vaults.")

        while True:
            try:
                loop_start = time.time()
                state.last_check = loop_start

                # 1. Fetch all safe vaults
                vaults = await fetch_kamino_vaults(client)
                if not vaults:
                    log.warning("No vaults returned. API issue? Sleeping...")
                    await asyncio.sleep(60)
                    continue

                best = vaults[0]
                log.info(f"Best overall: {best.name} | APY: {best.apy_7d:.2f}% | TVL: ${best.tvl_usd:,.0f} | Type: {best.vault_type}")
                log.info(f"Top 3: {[(v.name, f'{v.apy_7d:.1f}%', v.vault_type) for v in vaults[:3]]}")

                # In auto mode, find the best SOL reserve we can actually deposit into
                # (user holds SOL — can only deposit into SOL-denominated reserves)
                SOL_TOKENS = {"SOL", "WSOL"}
                best_reserve = next(
                    (v for v in vaults if v.vault_type == "reserve" and v.token.upper() in SOL_TOKENS),
                    None,
                )
                if can_execute and best_reserve:
                    log.info(f"Best auto-executable (SOL): {best_reserve.name} | APY: {best_reserve.apy_7d:.2f}% | Market: {best_reserve.market[:12]}...")
                    best = best_reserve
                elif can_execute:
                    log.warning("No SOL lending reserves found with positive APY. Monitoring only this cycle.")

                # 2. Fetch current position value
                position = await fetch_wallet_position(client, state.current_vault or best.address)
                state.position_usd = position
                log.info(f"Position: ${position:.4f} | Vault: {state.current_vault or 'none'} | Mode: {mode}")

                # 3. Decision logic
                current_apy = state.current_apy or 0.0
                do_migrate, reason = should_migrate(current_apy, best.apy_7d, position)

                def _update_state_vault(s: State, v: Vault):
                    s.current_vault = v.address
                    s.current_vault_type = v.vault_type
                    s.current_vault_market = v.market
                    s.current_vault_name = v.name
                    s.current_apy = v.apy_7d

                # Can only auto-execute on klend reserves (single-sided deposit)
                # Strategies need 2 tokens — monitor only
                can_auto_this = can_execute and best.vault_type == "reserve"

                if state.current_vault is None:
                    # ─── FIRST RUN ────────────────────────────────────────
                    log.info(f"Initial deployment → {best.name} ({best.vault_type}) at {best.apy_7d:.2f}% APY")

                    if can_auto_this:
                        success = await execute_initial_deposit(client, best)
                        if success:
                            _update_state_vault(state, best)
                            await telegram_alert(client,
                                f"AUTO: Initial deposit complete!\n"
                                f"Vault: <b>{best.name}</b> @ {best.apy_7d:.2f}% APY\n"
                                f"Address: {best.address}"
                            )
                        else:
                            log.warning("Initial deposit failed. Will retry next cycle.")
                            await telegram_alert(client, "Initial deposit failed. Retrying next cycle.")
                    else:
                        log.info(f"ACTION REQUIRED: Deposit into vault {best.address}")
                        _update_state_vault(state, best)
                        await telegram_alert(client,
                            f"Initial vault selected:\n<b>{best.name}</b> @ {best.apy_7d:.2f}% APY\n"
                            f"Deposit here:\nhttps://app.kamino.finance"
                        )

                elif do_migrate:
                    # ─── MIGRATION ────────────────────────────────────────
                    log.info(f"MIGRATE: {reason}")
                    log.info(f"  From: {state.current_vault_name} @ {current_apy:.2f}%")
                    log.info(f"  To:   {best.name} @ {best.apy_7d:.2f}%")

                    if can_auto_this:
                        # Reconstruct current vault object from state
                        current_v = Vault(
                            address=state.current_vault,
                            name=state.current_vault_name,
                            token="", apy_7d=current_apy, tvl_usd=0,
                            utilization=0, risk_score=0,
                            vault_type=state.current_vault_type,
                            market=state.current_vault_market,
                        )
                        success = await execute_migration(client, current_v, best)
                        if success:
                            _update_state_vault(state, best)
                            state.migrations += 1
                            projection = compute_compound_projection(position, best.apy_7d, 365)
                            await telegram_alert(client,
                                f"AUTO: Migration #{state.migrations} complete!\n"
                                f"Reason: {reason}\n"
                                f"New vault: <b>{best.name}</b> @ {best.apy_7d:.2f}%\n"
                                f"Projected 1yr: ${projection['end']:.2f} (+${projection['earned']:.2f})"
                            )
                        else:
                            log.error("Migration execution failed. Check logs and wallet.")
                            await telegram_alert(client,
                                f"Migration FAILED. Check wallet manually.\n"
                                f"Target was: {best.name} @ {best.apy_7d:.2f}%"
                            )
                    else:
                        _update_state_vault(state, best)
                        state.migrations += 1
                        projection = compute_compound_projection(position, best.apy_7d, 365)
                        await telegram_alert(client,
                            f"Migration #{state.migrations} — ACTION REQUIRED\n"
                            f"Reason: {reason}\n"
                            f"Move to: <b>{best.name}</b> @ {best.apy_7d:.2f}%\n"
                            f"Vault: {best.address}\n"
                            f"1yr projection: ${projection['end']:.2f} (+${projection['earned']:.2f})"
                        )

                else:
                    log.info(f"HOLD: {reason}")

                # 4. Projections
                if position > 0 and state.current_apy > 0:
                    p30  = compute_compound_projection(position, state.current_apy, 30)
                    p365 = compute_compound_projection(position, state.current_apy, 365)
                    log.info(f"Projection: 30d=${p30['end']:.4f} | 1yr=${p365['end']:.4f} | APY={state.current_apy:.2f}%")

                # 5. Save state
                save_state(state)
                consecutive_errors = 0

                # 6. Sleep
                elapsed = time.time() - loop_start
                sleep_for = max(10, POLL_INTERVAL_SECS - elapsed)
                log.info(f"Cycle done in {elapsed:.1f}s. Sleeping {sleep_for:.0f}s.\n")
                await asyncio.sleep(sleep_for)

            except KeyboardInterrupt:
                log.info("Shutdown requested. Saving state.")
                save_state(state)
                break

            except Exception as e:
                consecutive_errors += 1
                log.error(f"Error #{consecutive_errors}: {e}", exc_info=True)
                if consecutive_errors >= MAX_ERRORS:
                    await telegram_alert(client, f"FATAL: {consecutive_errors} consecutive errors. Agent stopped.")
                    log.critical("Too many errors. Exiting.")
                    break
                backoff = min(300, 30 * consecutive_errors)
                log.warning(f"Backing off {backoff}s...")
                await asyncio.sleep(backoff)


if __name__ == "__main__":
    asyncio.run(run())
