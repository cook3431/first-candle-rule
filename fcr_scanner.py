"""
fcr_scanner.py — Signal scanner. Runs every 60 seconds via cron.

Reads watchlist + first candles, fetches recent 5-min bars from Alpaca,
runs the full FCR strategy pipeline for each active stock, and writes
a pending signal file when a valid B-grade+ signal is found.

Cron (ET):
  2-59/1 10 * * 1-5  cd /path/to/trading-dashboard && python fcr_scanner.py
  *      11 * * 1-5  cd /path/to/trading-dashboard && python fcr_scanner.py
  *      12-14 * * 1-5  cd /path/to/trading-dashboard && python fcr_scanner.py  (PM KZ)
"""

import sys, os, json, logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

import pytz

from config import SystemConfig, MarketConfig
from models import Candle, Direction, SignalType
from strategy import FirstCandleStrategy
from data_client import fetch_intraday_bars

# ── Paths / config ────────────────────────────────────────────────────────────
ROOT      = Path(__file__).parent
STATE_DIR = ROOT / "state"
LOGS_DIR  = ROOT / "logs"
ET        = pytz.timezone("America/New_York")

# Risk per trade — read from env, default 500
RISK_DOLLARS = float(os.environ.get("FCR_RISK_DOLLARS", "500"))

# Stop scanning after 15:00 ET (no entries after this time)
STOP_SCAN_TIME = dtime(15, 0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SCANNER] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "scanner.log"),
    ],
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _set_phase(phase: str, **kwargs):
    existing = _read_json(STATE_DIR / "system-state.json") or {}
    existing.update({"phase": phase, "updated_at": datetime.now(ET).isoformat(), **kwargs})
    _write_json(STATE_DIR / "system-state.json", existing)


def _already_traded(symbol: str) -> bool:
    traded_file = STATE_DIR / "traded-today.txt"
    if not traded_file.exists():
        return False
    return symbol in traded_file.read_text().splitlines()


def _pending_exists(symbol: str) -> bool:
    return (STATE_DIR / "pending" / f"{symbol}.json").exists()


def _dict_to_candle(d: dict) -> Candle:
    from datetime import datetime as _dt
    dt = _dt.strptime(d["date"], "%Y-%m-%d")
    return Candle(
        timestamp=dt,
        open=float(d["open"]),  high=float(d["high"]),
        low=float(d["low"]),    close=float(d["close"]),
        volume=float(d.get("volume", 0)),
    )


# ── Per-stock scan ────────────────────────────────────────────────────────────

def scan_stock(stock: dict, fc_data: dict, now_et: datetime) -> Optional[dict]:
    """
    Run the full FCR pipeline for one stock.
    Returns a pending signal dict on B-grade+ signal, else None.
    """
    symbol    = stock["symbol"]
    trading_day = now_et.date()

    # Reconstruct first candle
    fc = fc_data.get(symbol)
    if not fc:
        log.debug(f"  {symbol}: no first candle data — skipping")
        return None

    first_candle_obj = Candle(
        timestamp=datetime.fromisoformat(fc["timestamp"]),
        open=fc["open"], high=fc["high"], low=fc["low"], close=fc["close"],
        volume=float(fc.get("volume", 0)),
    )

    # Fetch 5-min bars from 09:55 ET to now (covers full post-range period)
    try:
        fetch_start = datetime.combine(trading_day, dtime(9, 55))
        bars = fetch_intraday_bars(
            symbol=symbol,
            timeframe_minutes=5,
            start=fetch_start,
            end=now_et,
        )
    except Exception as e:
        log.error(f"  {symbol}: intraday fetch error — {e}")
        return None

    if not bars:
        log.debug(f"  {symbol}: no intraday bars yet")
        return None

    # Post-range candles only (10:00 ET onwards)
    post_range = [b for b in bars if b.timestamp.time() >= dtime(10, 0)]
    if len(post_range) < 3:
        log.debug(f"  {symbol}: only {len(post_range)} post-range bars — need ≥3")
        return None

    # ── Set up strategy ───────────────────────────────────────────────────────
    cfg = SystemConfig()
    cfg.market = MarketConfig(symbol=symbol, tick_size=0.01, tick_value=0.01, point_value=1.0)
    cfg.strategy.kill_zone_only = False
    cfg.strategy.skip_mondays   = True

    strategy = FirstCandleStrategy(cfg)
    strategy.reset_daily_state(datetime.combine(trading_day, dtime(0, 0)))

    # Restore bias from morning prep (avoids re-fetching daily bars)
    saved_bias = stock.get("bias", "NONE")
    if saved_bias in ("LONG", "SHORT"):
        strategy._bias = Direction[saved_bias]
        strategy._bias_strength = float(stock.get("bias_strength", 0.5))
    else:
        # No clear bias — skip
        return None

    # Restore liquidity levels
    if stock.get("prev_day_high") and stock.get("prev_day_low"):
        strategy.mark_liquidity_levels(
            prev_day_high=stock["prev_day_high"],
            prev_day_low=stock["prev_day_low"],
        )
    if stock.get("session_open_price"):
        strategy.set_session_open_price(stock["session_open_price"])

    # Mark first candle range
    strategy.mark_first_candle(first_candle_obj)

    # ── Run candle loop ───────────────────────────────────────────────────────
    candle_buffer = []
    found_signal  = None

    for candle in post_range:
        if candle.timestamp.time() >= STOP_SCAN_TIME:
            break
        candle_buffer.append(candle)
        if len(candle_buffer) < 3:
            continue

        sig = strategy.generate_signal(candle.timestamp, candle_buffer)
        if sig.signal_type in (SignalType.ENTER_LONG, SignalType.ENTER_SHORT):
            found_signal = sig
            break

    if found_signal is None:
        displacement = strategy.range_broken_high or strategy.range_broken_low
        log.debug(f"  {symbol}: no signal  displacement={'yes' if displacement else 'no'}")
        return None

    # ── Confidence gate — B-grade (≥60) or better only ───────────────────────
    conf = found_signal.confidence
    if conf is None or conf.score < 60:
        grade = conf.grade.value if conf else "?"
        score = conf.score if conf else 0
        log.info(f"  {symbol}: signal found but grade {grade}/{score} < 60 — skipped (TRAP/C/D)")
        return None

    # ── Build pending signal ──────────────────────────────────────────────────
    today_noon_et = datetime.combine(trading_day, dtime(15, 0))

    pending = {
        "symbol":           symbol,
        "direction":        found_signal.direction.value,
        "entry_price":      round(found_signal.entry_price, 4),
        "stop_loss":        round(found_signal.stop_loss, 4),
        "take_profit":      round(found_signal.take_profit, 4),
        "trail_mode":       True,         # Mode B — always trail
        "risk_per_trade":   RISK_DOLLARS,
        "confidence_grade": conf.grade.value,
        "confidence_score": conf.score,
        "first_candle_high":round(fc["high"], 4),
        "first_candle_low": round(fc["low"],  4),
        "fvg_top":    round(found_signal.fvg.top,    4) if found_signal.fvg else None,
        "fvg_bottom": round(found_signal.fvg.bottom, 4) if found_signal.fvg else None,
        "generated_at": datetime.now(ET).isoformat(),
        "expires_at":   today_noon_et.isoformat(),
    }

    log.info(
        f"  {symbol}: SIGNAL {found_signal.direction.value} "
        f"entry={found_signal.entry_price:.2f} "
        f"sl={found_signal.stop_loss:.2f} "
        f"grade={conf.grade.value} score={conf.score}"
    )
    return pending


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now_et = datetime.now(ET)

    # ── Phase gate ────────────────────────────────────────────────────────────
    state = _read_json(STATE_DIR / "system-state.json") or {}
    phase = state.get("phase", "IDLE")
    if phase not in ("SCANNING", "SIGNAL_PENDING"):
        log.debug(f"Phase is {phase} — scanner not active, exiting")
        return

    # ── Time gate ─────────────────────────────────────────────────────────────
    if now_et.time() >= STOP_SCAN_TIME:
        log.info(f"Past {STOP_SCAN_TIME} ET — no more entries. Phase → DONE")
        _set_phase("DONE")
        return

    # ── Load state files ──────────────────────────────────────────────────────
    wl = _read_json(STATE_DIR / "watchlist-today.json")
    fc = _read_json(STATE_DIR / "first-candles-today.json")

    if not wl or not fc:
        log.error("watchlist or first-candles file missing — run fcr_morning.py and fcr_first_candle.py first")
        return

    active_stocks = [s for s in wl.get("stocks", []) if not s.get("skip_today")]
    fc_candles    = fc.get("candles", {})

    if not active_stocks:
        log.info("No active stocks in watchlist")
        return

    log.info(f"Scanning {len(active_stocks)} stocks  {now_et.strftime('%H:%M %Z')}")

    # ── Kill zone filter — only scan during kill zones ────────────────────────
    t = now_et.time()
    in_am_kz = dtime(10, 0) <= t < dtime(11, 0)
    in_pm_kz = dtime(14, 0) <= t < dtime(15, 0)
    in_mid   = dtime(11, 0) <= t < dtime(14, 0)

    if in_mid:
        log.debug("Mid-session — no new entries. Scanner idle until 14:00 ET.")
        return

    signals_found = 0

    for stock in active_stocks:
        symbol = stock["symbol"]

        if _already_traded(symbol):
            log.debug(f"  {symbol}: already traded today — skipping")
            continue

        if _pending_exists(symbol):
            log.debug(f"  {symbol}: pending signal already exists — executor handling it")
            continue

        pending = scan_stock(stock, fc_candles, now_et)

        if pending:
            pending_path = STATE_DIR / "pending" / f"{symbol}.json"
            _write_json(pending_path, pending)
            signals_found += 1
            log.info(f"  → Pending signal written: {pending_path.name}")
            _set_phase("SIGNAL_PENDING", pending_symbols=[symbol])

    if signals_found == 0:
        log.info(f"  Scan complete — no new signals")
    else:
        log.info(f"  Scan complete — {signals_found} new signal(s) written to state/pending/")


if __name__ == "__main__":
    main()
