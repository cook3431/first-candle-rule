"""
fcr_morning.py — Daily morning preparation.

Fires at 08:00 ET (13:00 GMT / 14:00 BST) Monday–Friday via cron.
Cleans yesterday's ephemeral state, fetches daily bias for all watchlist
stocks via Alpaca, and writes state/watchlist-today.json.

Run manually:   python fcr_morning.py
Cron (ET):      0 8 * * 1-5  cd /path/to/trading-dashboard && python fcr_morning.py
"""

import sys, os, json, logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, time as dtime
from pathlib import Path

import pytz

from config import SystemConfig, MarketConfig
from models import Candle, Direction
from strategy import FirstCandleStrategy
from data_client import fetch_daily_bars

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT      = Path(__file__).parent
STATE_DIR = ROOT / "state"
LOGS_DIR  = ROOT / "logs"
TRADES_DIR= ROOT / "trades"

LOGS_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)
(STATE_DIR / "pending").mkdir(exist_ok=True)
TRADES_DIR.mkdir(exist_ok=True)

# ── Config ────────────────────────────────────────────────────────────────────
ET        = pytz.timezone("America/New_York")
WATCHLIST = ["QQQ", "SPY", "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD"]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MORNING] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "morning.log"),
    ],
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_json(path: Path, data: dict):
    """Atomic write via temp file → rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def _set_phase(phase: str, extra: dict = None):
    payload = {
        "phase": phase,
        "updated_at": datetime.now(ET).isoformat(),
        "active_symbol": None,
        "pending_symbols": [],
        **(extra or {}),
    }
    _write_json(STATE_DIR / "system-state.json", payload)


def _clean_state():
    """Wipe yesterday's ephemeral state files (leaves trades/ untouched)."""
    for name in [
        "first-candles-today.json", "active-trade.json",
        "traded-today.txt", "exit_monitor.pid", "executor.pid",
    ]:
        p = STATE_DIR / name
        if p.exists():
            p.unlink()
    for f in (STATE_DIR / "pending").glob("*.json"):
        f.unlink()
    log.info("Stale state files cleared")


def _candle_to_dict(c: Candle) -> dict:
    return {
        "date":   c.timestamp.date().isoformat(),
        "open":   round(c.open,   4),
        "high":   round(c.high,   4),
        "low":    round(c.low,    4),
        "close":  round(c.close,  4),
        "volume": int(c.volume),
    }


def _dict_to_candle(d: dict) -> Candle:
    from datetime import date as _date, datetime as _dt
    dt = _dt.strptime(d["date"], "%Y-%m-%d")
    return Candle(
        timestamp=dt,
        open=float(d["open"]),
        high=float(d["high"]),
        low=float(d["low"]),
        close=float(d["close"]),
        volume=float(d.get("volume", 0)),
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now_et       = datetime.now(ET)
    trading_day  = now_et.date()

    log.info(f"{'='*50}")
    log.info(f"MORNING PREP  {trading_day}  ({now_et.strftime('%H:%M %Z')})")
    log.info(f"{'='*50}")

    _set_phase("MORNING_PREP")

    # ── Monday skip ──────────────────────────────────────────────────────────
    if now_et.weekday() == 0:
        log.info("Monday — no trades today (weekly cycle rule). Marking all stocks skip.")
        _write_json(STATE_DIR / "watchlist-today.json", {
            "generated_at": now_et.isoformat(),
            "trading_day":  str(trading_day),
            "skip_reason":  "Monday",
            "stocks":       [],
        })
        _set_phase("DONE", {"skip_reason": "Monday"})
        return

    _clean_state()

    # ── Fetch daily bars (single multi-symbol call) ───────────────────────────
    log.info(f"Fetching daily bars for {len(WATCHLIST)} stocks via Alpaca…")
    try:
        all_bars = fetch_daily_bars(WATCHLIST, lookback_days=30)
    except Exception as e:
        log.error(f"Fatal: Alpaca daily bars fetch failed: {e}")
        sys.exit(1)

    stocks_out = []

    for rank, symbol in enumerate(WATCHLIST, start=1):
        try:
            bars = all_bars.get(symbol, [])
            # Only confirmed daily candles (strictly before today)
            confirmed = [b for b in bars if b.timestamp.date() < trading_day]

            if len(confirmed) < 5:
                log.warning(f"{symbol}: only {len(confirmed)} confirmed daily bars — skipping")
                stocks_out.append({
                    "symbol": symbol, "bias": "NONE", "bias_strength": 0.0,
                    "prev_day_high": None, "prev_day_low": None, "prev_day_close": None,
                    "prev_week_high": None, "prev_week_low": None,
                    "session_open_price": None, "watchlist_rank": rank,
                    "skip_today": True, "skip_reason": "Insufficient daily data",
                    "recent_candles": [],
                })
                continue

            recent5 = confirmed[-5:]
            prev_day = confirmed[-1]

            # Run strategy on saved data
            cfg = SystemConfig()
            cfg.market = MarketConfig(symbol=symbol, tick_size=0.01, tick_value=0.01, point_value=1.0)
            cfg.strategy.kill_zone_only = False
            strategy = FirstCandleStrategy(cfg)
            strategy.reset_daily_state(datetime.combine(trading_day, dtime(0, 0)))

            bias = strategy.determine_bias(recent5)
            bias_strength = float(getattr(strategy, "_bias_strength", 0.5))

            strategy.mark_liquidity_levels(
                prev_day_high=prev_day.high,
                prev_day_low=prev_day.low,
            )
            strategy.set_session_open_price(prev_day.close)

            skip  = bias == Direction.NONE
            entry = {
                "symbol":            symbol,
                "bias":              bias.value,
                "bias_strength":     round(bias_strength, 3),
                "prev_day_high":     round(prev_day.high, 4),
                "prev_day_low":      round(prev_day.low, 4),
                "prev_day_close":    round(prev_day.close, 4),
                "prev_week_high":    round(max(c.high for c in recent5), 4),
                "prev_week_low":     round(min(c.low  for c in recent5), 4),
                "session_open_price":round(prev_day.close, 4),
                "watchlist_rank":    rank,
                "skip_today":        skip,
                "skip_reason":       "No clear bias" if skip else None,
                # Save raw candles so scanner can reconstruct strategy state
                # without making another API call
                "recent_candles":    [_candle_to_dict(c) for c in recent5],
            }
            stocks_out.append(entry)

            status = "SKIP (no bias)" if skip else f"bias={bias.value} ({bias_strength:.2f})"
            log.info(f"  {symbol:6s}  {status}  PDH={prev_day.high:.2f}  PDL={prev_day.low:.2f}")

        except Exception as e:
            log.error(f"  {symbol}: error — {e}")
            stocks_out.append({
                "symbol": symbol, "bias": "NONE", "bias_strength": 0.0,
                "prev_day_high": None, "prev_day_low": None, "prev_day_close": None,
                "prev_week_high": None, "prev_week_low": None,
                "session_open_price": None, "watchlist_rank": rank,
                "skip_today": True, "skip_reason": str(e),
                "recent_candles": [],
            })

    watchable = sum(1 for s in stocks_out if not s["skip_today"])
    _write_json(STATE_DIR / "watchlist-today.json", {
        "generated_at": now_et.isoformat(),
        "trading_day":  str(trading_day),
        "stocks":       stocks_out,
    })

    log.info(f"Watchlist written — {watchable}/{len(WATCHLIST)} stocks active today")
    log.info("Morning prep complete. Next: fcr_first_candle.py fires at 10:01 ET.")


if __name__ == "__main__":
    main()
