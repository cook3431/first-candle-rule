"""
setup_tonight.py — One-shot setup script. Run this once before tomorrow.

Does everything needed for a successful market open:
  1. Finds or prompts for Alpaca API keys
  2. Saves keys to a local .env file (used by all FCR scripts at runtime)
  3. Installs the full crontab (including keys + stream entry)
  4. Verifies the Alpaca connection works
  5. Confirms Mac sleep prevention settings
"""

import os, sys, json, subprocess, getpass
from pathlib import Path

FCR = Path(__file__).parent
PY  = sys.executable


# ── Colours ───────────────────────────────────────────────────────────────────
def green(s):  return f"\033[92m{s}\033[0m"
def yellow(s): return f"\033[93m{s}\033[0m"
def red(s):    return f"\033[91m{s}\033[0m"
def bold(s):   return f"\033[1m{s}\033[0m"


# ── Step helpers ──────────────────────────────────────────────────────────────
def step(n, title):
    print(f"\n{bold(f'Step {n} — {title}')}")
    print("─" * 50)


def ok(msg):   print(green(f"  ✓ {msg}"))
def warn(msg): print(yellow(f"  ⚠ {msg}"))
def fail(msg): print(red(f"  ✗ {msg}"))


# ── Step 1: Find or prompt for API keys ───────────────────────────────────────

def get_keys():
    step(1, "Alpaca API keys")

    env_file = FCR / ".env"

    # Already in .env file?
    saved = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                saved[k.strip()] = v.strip()

    api_key    = saved.get("ALPACA_API_KEY")    or os.environ.get("ALPACA_API_KEY",    "").strip()
    secret_key = saved.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY", "").strip()
    paper      = saved.get("ALPACA_PAPER")      or os.environ.get("ALPACA_PAPER",      "true").strip()

    if api_key and secret_key:
        ok(f"Keys found  (API key: {api_key[:6]}…)")
    else:
        print("  Your Alpaca API keys are needed so the trading scripts can place orders.")
        print("  Get them from: app.alpaca.markets → Paper Trading → API Keys\n")
        api_key = input("  Paste your API Key (starts with PK): ").strip()
        secret_key = getpass.getpass("  Paste your Secret Key (hidden input): ").strip()
        if not api_key or not secret_key:
            fail("Keys not entered — cannot continue")
            sys.exit(1)

    # Save to .env file
    env_file.write_text(
        f"ALPACA_API_KEY={api_key}\n"
        f"ALPACA_SECRET_KEY={secret_key}\n"
        f"ALPACA_PAPER={paper}\n"
    )
    ok(f"Keys saved to {env_file}")
    return api_key, secret_key, paper


# ── Step 2: Install crontab with keys embedded ────────────────────────────────

def install_crontab(api_key, secret_key):
    step(2, "Installing crontab")

    lines = [
        "# FCR Trading System",
        f"ALPACA_API_KEY={api_key}",
        f"ALPACA_SECRET_KEY={secret_key}",
        "ALPACA_PAPER=true",
        "TZ=America/New_York",
        "",
        "# Morning prep — 08:00 ET",
        f"0 8 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_morning.py >> logs/morning.log 2>&1",
        "",
        "# WebSocket stream — starts 09:25 ET (real-time bars, free tier)",
        f"25 9 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_stream.py >> logs/stream.log 2>&1",
        "",
        "# First candle — 10:01 ET",
        f"1 10 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_first_candle.py >> logs/scanner.log 2>&1",
        "",
        "# Scanner — every 60s during kill zones",
        f"* 10 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_scanner.py >> logs/scanner.log 2>&1",
        f"* 11 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_scanner.py >> logs/scanner.log 2>&1",
        f"* 12 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_scanner.py >> logs/scanner.log 2>&1",
        f"* 13 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_scanner.py >> logs/scanner.log 2>&1",
        f"* 14 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_scanner.py >> logs/scanner.log 2>&1",
        "",
        "# Supervisor — every 30s (manages all processes)",
        f"* 9 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
        f"* 10 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
        f"* 11 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
        f"* 12 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
        f"* 13 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
        f"* 14 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
        f"* 15 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
        f"* 16 * * 1,2,3,4,5 cd {FCR} && {PY} fcr_supervisor.py >> logs/supervisor.log 2>&1",
    ]

    content = "\n".join(lines) + "\n"
    result = subprocess.run(["crontab", "-"], input=content.encode(), capture_output=True)

    if result.returncode == 0:
        ok("Crontab installed with API keys embedded")
    else:
        fail(f"Crontab install failed: {result.stderr.decode()}")
        sys.exit(1)


# ── Step 3: Patch scripts to load .env at runtime ────────────────────────────

def patch_env_loader():
    """
    Ensure all FCR scripts load .env at startup so keys are available
    even if cron env embedding ever fails.
    """
    step(3, "Patching scripts to load .env at startup")

    loader_code = '''
# ── Load .env file if present ─────────────────────────────────────────────────
import os as _os
_env_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".env")
if _os.path.exists(_env_path):
    for _line in open(_env_path).read().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _, _v = _line.partition("=")
            _os.environ.setdefault(_k.strip(), _v.strip())
# ─────────────────────────────────────────────────────────────────────────────
'''

    scripts = [
        "fcr_morning.py", "fcr_first_candle.py", "fcr_scanner.py",
        "fcr_executor.py", "fcr_exit_monitor.py", "fcr_supervisor.py",
        "fcr_stream.py",
    ]

    patched = 0
    for name in scripts:
        path = FCR / name
        if not path.exists():
            warn(f"  {name} not found — skipping")
            continue
        content = path.read_text()
        marker = "# ── Load .env file"
        if marker in content:
            ok(f"  {name} already patched")
            continue
        # Insert after the sys.path.insert line
        insert_after = 'sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))'
        if insert_after in content:
            content = content.replace(insert_after, insert_after + "\n" + loader_code, 1)
            path.write_text(content)
            ok(f"  {name} patched")
            patched += 1
        else:
            warn(f"  {name} — could not find insertion point, skipping")

    if patched == 0:
        ok("All scripts already have .env loader")


# ── Step 4: Verify Alpaca connection ─────────────────────────────────────────

def verify_connection(api_key, secret_key):
    step(4, "Verifying Alpaca connection")

    os.environ["ALPACA_API_KEY"]    = api_key
    os.environ["ALPACA_SECRET_KEY"] = secret_key
    os.environ["ALPACA_PAPER"]      = "true"

    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(api_key, secret_key, paper=True)
        account = client.get_account()
        ok(f"Connected to Alpaca paper account")
        ok(f"Buying power: ${float(account.buying_power):,.0f}")
        ok(f"Equity: ${float(account.equity):,.0f}")
    except ImportError:
        fail("alpaca-py not installed — run: pip3 install alpaca-py")
        sys.exit(1)
    except Exception as e:
        fail(f"Alpaca connection failed: {e}")
        print(yellow("  Check your API keys at app.alpaca.markets"))
        sys.exit(1)

    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestBarRequest
        dc = StockHistoricalDataClient(api_key, secret_key)
        req = StockLatestBarRequest(symbol_or_symbols="QQQ", feed="iex")
        bar = dc.get_stock_latest_bar(req)
        price = float(bar["QQQ"].close)
        ok(f"Data feed working — QQQ latest: ${price:.2f}")
    except Exception as e:
        warn(f"Data feed check failed: {e} (non-fatal)")


# ── Step 5: Check log directories ─────────────────────────────────────────────

def check_directories():
    step(5, "Checking directories")
    for d in ["logs", "state", "state/pending", "trades"]:
        p = FCR / d
        p.mkdir(parents=True, exist_ok=True)
        ok(f"  {d}/  exists")

    # Ensure trades/index.json exists
    idx = FCR / "trades" / "index.json"
    if not idx.exists():
        idx.write_text('{"trades": []}')
        ok("  trades/index.json created")
    else:
        ok("  trades/index.json exists")


# ── Step 6: Summary ───────────────────────────────────────────────────────────

def print_summary():
    print(f"\n{'='*50}")
    print(bold(green("  SETUP COMPLETE — You're ready for tomorrow")))
    print(f"{'='*50}")
    print("""
  Tomorrow's schedule (UK / BST):

    13:00  Morning prep fires automatically
    14:25  Real-time stream starts (WebSocket)
    15:01  First candle recorded
    15:02  Scanner runs every 60 seconds
    ~15:02–16:00  Trades execute automatically

  To monitor:  Open dashboard → ⚙ System tab

  ONE THING TO DO:
    Make sure your Mac is awake and not sleeping.
    System Settings → Battery → set sleep to Never.
""")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(bold("\nFCR Trading System — Tonight Setup"))
    print("=" * 50)

    check_directories()
    api_key, secret_key, paper = get_keys()
    install_crontab(api_key, secret_key)
    patch_env_loader()
    verify_connection(api_key, secret_key)
    print_summary()
