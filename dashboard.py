"""
REAPER DASHBOARD — Modern Terminal UI
Run alongside the agent: python3 dashboard.py
Reads reaper_state.json and reaper.log live.
"""

import json, os, time, sys, shutil
from datetime import timedelta

STATE_FILE = "reaper_state.json"
LOG_FILE   = "reaper.log"

# ─── THEME ───────────────────────────────────────────────────────────────────

RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
ITALIC  = "\033[3m"

# Gradient palette — cool neon on dark
BG      = "\033[48;2;13;13;28m"       # deep navy bg
FG      = "\033[38;2;200;200;220m"    # soft white
ACCENT  = "\033[38;2;0;255;180m"      # neon mint
ACCENT2 = "\033[38;2;100;140;255m"    # electric blue
WARN    = "\033[38;2;255;200;60m"     # amber
ERR     = "\033[38;2;255;80;80m"      # soft red
MUTED   = "\033[38;2;80;80;110m"      # muted grey-blue
PROFIT  = "\033[38;2;0;230;140m"      # green profit
LABEL   = "\033[38;2;140;140;170m"    # label grey

# Box drawing (rounded)
TL = "╭"; TR = "╮"; BL = "╰"; BR = "╯"
H  = "─"; V  = "│"

def clear():
    os.system("clear" if os.name != "nt" else "cls")

def read_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def read_last_logs(n=8) -> list[str]:
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return ["  waiting for agent..."]

def compound(principal: float, apy: float, days: int) -> float:
    daily = apy / 100 / 365
    return principal * ((1 + daily) ** days)

def format_uptime(started_at: float) -> str:
    delta = timedelta(seconds=time.time() - started_at)
    d = delta.days
    h, r = divmod(int(delta.total_seconds()) % 86400, 3600)
    m, s = divmod(r, 60)
    if d > 0:
        return f"{d}d {h}h {m}m"
    return f"{h}h {m}m {s}s"

def spark_bar(value: float, max_val: float, width: int = 16) -> str:
    """Render a mini spark bar."""
    if max_val <= 0:
        return f"{MUTED}{'░' * width}{RESET}"
    filled = int(min(1.0, value / max_val) * width)
    return f"{ACCENT}{'█' * filled}{MUTED}{'░' * (width - filled)}{RESET}"

def colorize_log(line: str) -> str:
    if "[WARNING]" in line or "HOLD:" in line:
        return f"{WARN}{line}{RESET}"
    if "[ERROR]" in line or "FATAL" in line:
        return f"{ERR}{line}{RESET}"
    if "MIGRATE" in line:
        return f"{ACCENT}{BOLD}{line}{RESET}"
    if "Best vault" in line:
        return f"{ACCENT2}{line}{RESET}"
    return f"{MUTED}{line}{RESET}"

def box_line(left: str, content: str, right: str, width: int) -> str:
    visible_len = len(content.encode('ascii', 'ignore').decode())
    # Strip ANSI for length calc
    import re
    clean = re.sub(r'\033\[[^m]*m', '', content)
    pad = width - 2 - len(clean)
    if pad < 0:
        pad = 0
    return f"{MUTED}{left}{RESET}{content}{' ' * pad}{MUTED}{right}{RESET}"

def render(state: dict, logs: list[str]):
    cols = min(shutil.get_terminal_size().columns, 64)
    w = cols - 4  # inner width

    clear()

    pos    = state.get("position_usd", 0)
    apy    = state.get("current_apy", 0)
    vault  = state.get("current_vault") or "none"
    vault_short = vault[:24] + "..." if len(vault) > 24 else vault
    migs   = state.get("migrations", 0)
    earned = state.get("total_earned_usd", 0)
    start  = state.get("started_at", time.time())
    last   = state.get("last_check", 0)

    p7    = compound(pos, apy, 7)
    p30   = compound(pos, apy, 30)
    p365  = compound(pos, apy, 365)

    last_ago = int(time.time() - last) if last else 0
    status = f"{ACCENT}LIVE" if last_ago < 360 else f"{WARN}STALE"

    # ─── HEADER ──────────────────────────────────────────────────────────────

    print()
    print(f"  {MUTED}{TL}{H * (w)}{TR}{RESET}")
    print(f"  {MUTED}{V}{RESET}  {ACCENT}{BOLD}KAMINO YIELD REAPER{RESET}                   {status}{RESET}  {MUTED}{V}{RESET}")
    print(f"  {MUTED}{V}{H * (w)}{V}{RESET}")

    # ─── POSITION CARD ───────────────────────────────────────────────────────

    print(f"  {MUTED}{V}{RESET}")
    print(f"  {MUTED}{V}{RESET}  {LABEL}POSITION{RESET}             {LABEL}APY{RESET}")
    print(f"  {MUTED}{V}{RESET}  {BOLD}{FG}${pos:<20.4f}{RESET} {ACCENT}{BOLD}{apy:.2f}%{RESET}")
    print(f"  {MUTED}{V}{RESET}  {spark_bar(apy, 20)}")
    print(f"  {MUTED}{V}{RESET}")
    print(f"  {MUTED}{V}{RESET}  {LABEL}VAULT{RESET}")
    print(f"  {MUTED}{V}{RESET}  {MUTED}{vault_short}{RESET}")
    print(f"  {MUTED}{V}{RESET}")

    # ─── STATS ROW ───────────────────────────────────────────────────────────

    print(f"  {MUTED}{V}{RESET}  {LABEL}MIGRATIONS{RESET}  {FG}{migs}{RESET}    {LABEL}EARNED{RESET}  {PROFIT}${earned:.4f}{RESET}    {LABEL}UPTIME{RESET}  {FG}{format_uptime(start)}{RESET}")
    print(f"  {MUTED}{V}{RESET}  {LABEL}LAST CHECK{RESET}  {status} {MUTED}{last_ago}s ago{RESET}")

    # ─── PROJECTIONS ─────────────────────────────────────────────────────────

    print(f"  {MUTED}{V}{RESET}")
    print(f"  {MUTED}{V}{H * (w)}{V}{RESET}")
    print(f"  {MUTED}{V}{RESET}  {ACCENT2}{BOLD}PROJECTIONS{RESET}")
    print(f"  {MUTED}{V}{RESET}")
    print(f"  {MUTED}{V}{RESET}  {LABEL}7d{RESET}    ${p7:<12.4f}  {PROFIT}+${p7-pos:.4f}{RESET}")
    print(f"  {MUTED}{V}{RESET}  {LABEL}30d{RESET}   ${p30:<12.4f}  {PROFIT}+${p30-pos:.4f}{RESET}")
    print(f"  {MUTED}{V}{RESET}  {LABEL}1yr{RESET}   ${p365:<12.4f}  {PROFIT}+${p365-pos:.4f}{RESET}")

    # ─── LOG ─────────────────────────────────────────────────────────────────

    print(f"  {MUTED}{V}{RESET}")
    print(f"  {MUTED}{V}{H * (w)}{V}{RESET}")
    print(f"  {MUTED}{V}{RESET}  {ACCENT2}{BOLD}LOG{RESET}")
    print(f"  {MUTED}{V}{RESET}")
    for line in logs:
        trimmed = line[:w - 4]
        print(f"  {MUTED}{V}{RESET}  {colorize_log(trimmed)}")

    # ─── FOOTER ──────────────────────────────────────────────────────────────

    print(f"  {MUTED}{V}{RESET}")
    print(f"  {MUTED}{BL}{H * (w)}{BR}{RESET}")
    print(f"     {MUTED}{ITALIC}refreshing every 10s  {DIM}ctrl+c to exit{RESET}")
    print()


def main():
    print(f"\n  {ACCENT}Starting dashboard...{RESET}\n")
    time.sleep(0.5)
    while True:
        try:
            state = read_state()
            logs  = read_last_logs(8)
            render(state, logs)
            time.sleep(10)
        except KeyboardInterrupt:
            print(f"\n  {MUTED}Dashboard closed.{RESET}\n")
            sys.exit(0)

if __name__ == "__main__":
    main()
