"""
fcr_first_candle.py — Record the 09:30–10:00 ET first candle for all watchlist stocks.

Fires at 10:01 ET (15:01 GMT / 16:01 BST) Monday–Friday via cron.
Reads state/watchlist-today.json, fetches the 30-min candle at 09:30 ET
via Alpaca, writes state/first-candles-today.json, and sets phase: SCANNING.

Run manually:   python fcr_first_candle.py
Cron (ET):      1 10 * * 1-5  cd /path/to/trading-dashboard && python fcr_first_candle.py
"""

import sys, os, json, logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import pytz

from config import SystemConfig, MarketConfig
from models import Candle
from strategy import FirstCandleStrategy
from data_client import fetch_intraday_bars

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT      = Path(__file__).parent
STATE_DIR = ROOT / "state"
LOGS_DIR  = ROOT / "logs"
ET        = pytz.timezone("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FIRST_CANDLE] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "scanner.log"),
    ],
)
log = logging.getLogger(__name__)


def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def _set_phase(phase: str):
    try:
        existing = json.loads((STATE_DIR / "system-state.json").read_text())
    except Exception:
        existing = {}
    existing.update({"phase": phase, "updated_at": datetime.now(ET).isoformat()})
    _write_json(STATE_DIR / "system-state.json", existing)


def main():
    now_et      = datetime.now(ET)
    trading_day = now_et.date()

    log.info(f"{'='*50}")
    log.info(f"FIRST CANDLE  {trading_day}  ({now_et.strftime('%H:%M %Z')})")
    log.info(f"{'='*50}")

    # ── Load watchlist ────────────────────────────────────────────────────────
    wl_path = STATE_DIR / "watchlist-today.json"
    if not wl_path.exists():
        log.error("watchlist-today.json not found — did fcr_morning.py run?")
        sys.exit(1)

    wl = json.loads(wl_path.read_text())
    active_stocks = [s for s in wl.get("stocks", []) if not s.get("skip_today")]

    if not active_stocks:
        log.info("No active stocks in watchlist — setting phase DONE")
        _set_phase("DONE")
        return

    # ── Fetch the 09:30 30-min candle for each stock ──────────────────────────
    # Request window: 09:25–10:05 ET to be sure we capture the 09:30 bar
    fetch_start = datetime.combine(trading_day, dtime(9, 25))
    fetch_end   = datetime.combine(trading_day, dtime(10, 5))

    candles_out = {}
    for stock in active_stocks:
        symbol = stock["symbol"]
        try:
            bars = fetch_intraday_bars(
                symbol=symbol,
                timeframe_minutes=30,
                start=fetch_start,
                end=fetch_end,
            )
            # Find the bar that opened at 09:30
            fc_bar = next(
                (b for b in bars if b.timestamp.hour == 9 and b.timestamp.minute == 30),
                None,
            )
            if fc_bar is None:
                log.warning(f"{symbol}: no 09:30 bar found (bars returned: {len(bars)})")
                continue

            candles_out[symbol] = {
                "high":      round(fc_bar.high,  4),
                "low":       round(fc_bar.low,   4),
                "open":      round(fc_bar.open,  4),
                "close":     round(fc_bar.close, 4),
                "volume":    int(fc_bar.volume),
                "range":     round(fc_bar.high - fc_bar.low, 4),
                "midpoint":  round((fc_bar.high + fc_bar.low) / 2, 4),
                "timestamp": fc_bar.timestamp.isoformat(),
            }
            log.info(f"  {symbol:6s}  H={fc_bar.high:.2f}  L={fc_bar.low:.2f}  Range={fc_bar.high-fc_bar.low:.2f}")

        except Exception as e:
            log.error(f"  {symbol}: error fetching first candle — {e}")

    if not candles_out:
        log.error("No first candles retrieved — scanner cannot run without this data")
        sys.exit(1)

    _write_json(STATE_DIR / "first-candles-today.json", {
        "generated_at": now_et.isoformat(),
        "trading_day":  str(trading_day),
        "candles":      candles_out,
    })

    log.info(f"First candles written for {len(candles_out)}/{len(active_stocks)} stocks")
    _set_phase("SCANNING")
    log.info("Phase → SCANNING. fcr_scanner.py will now run every 60 seconds.")


if __name__ == "__main__":
    main()
