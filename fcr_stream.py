"""
fcr_stream.py — Real-time WebSocket bar stream via Alpaca StockDataStream.

The free Alpaca tier has a 15-minute delay on REST API calls. Only the
WebSocket stream is real-time on the free tier. This process subscribes
to 1-minute bars, aggregates them into 5-minute bars, and writes two files
that the scanner and exit monitor read from:

  state/stream-bars.json    — rolling 5-min bars per symbol (real-time)
  state/latest-prices.json  — most recent price per symbol (real-time)

Launched by fcr_supervisor.py at market open. Exits automatically at 16:05 ET.
"""

import sys, os, json, logging, time, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, time as dtime
from pathlib import Path
from collections import defaultdict
from typing import Dict, List

import pytz

ROOT      = Path(__file__).parent
STATE_DIR = ROOT / "state"
LOGS_DIR  = ROOT / "logs"
ET        = pytz.timezone("America/New_York")

WATCHLIST  = ["QQQ", "SPY", "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD"]
BAR_WINDOW = 50   # Keep last N 5-min bars per symbol in memory
STREAM_START = dtime(9, 25)
STREAM_STOP  = dtime(16, 5)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [STREAM] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "stream.log"),
    ],
)
log = logging.getLogger(__name__)

# ── In-memory state (written to files after every bar) ────────────────────────
_five_min_bars: Dict[str, List[dict]] = defaultdict(list)
_latest_prices: Dict[str, dict]       = {}
_write_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _five_min_window(ts: datetime) -> str:
    """Return ISO string of the 5-minute window a timestamp falls in."""
    rounded = ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)
    return rounded.isoformat()


def _write_state_files():
    """Atomically write bars and prices to state files."""
    with _write_lock:
        for path, data in [
            (STATE_DIR / "stream-bars.json",    _five_min_bars),
            (STATE_DIR / "latest-prices.json",  _latest_prices),
        ]:
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2, default=str))
                tmp.replace(path)
            except Exception as e:
                log.warning(f"Could not write {path.name}: {e}")


def _ingest_one_min_bar(symbol: str, ts: datetime, o: float, h: float, l: float, c: float, v: float):
    """Merge a 1-min bar into the running 5-min bar for this symbol."""
    window_key = _five_min_window(ts)
    bars = _five_min_bars[symbol]

    existing = next((b for b in bars if b["timestamp"] == window_key), None)
    if existing is None:
        bars.append({
            "timestamp": window_key,
            "open":   o,
            "high":   h,
            "low":    l,
            "close":  c,
            "volume": v,
        })
        # Trim to window size
        if len(bars) > BAR_WINDOW:
            _five_min_bars[symbol] = bars[-BAR_WINDOW:]
    else:
        existing["high"]   = max(existing["high"], h)
        existing["low"]    = min(existing["low"],  l)
        existing["close"]  = c
        existing["volume"] = existing.get("volume", 0) + v

    _latest_prices[symbol] = {"price": c, "timestamp": ts.isoformat()}


async def _handle_bar(bar):
    """Async callback — called by StockDataStream on each new 1-min bar."""
    try:
        symbol = bar.symbol
        ts = bar.timestamp
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        if ts.tzinfo is not None:
            ts = ts.astimezone(ET).replace(tzinfo=None)

        _ingest_one_min_bar(
            symbol, ts,
            float(bar.open), float(bar.high), float(bar.low), float(bar.close),
            float(bar.volume) if bar.volume else 0.0,
        )
        _write_state_files()
        log.debug(f"  {symbol} @ {float(bar.close):.2f}  {ts.strftime('%H:%M')}")

    except Exception as e:
        log.warning(f"Bar handler error ({getattr(bar, 'symbol', '?')}): {e}")


# ── EOD watchdog ──────────────────────────────────────────────────────────────

def _eod_watchdog(wss):
    """Background thread — stops the stream at STREAM_STOP time."""
    while True:
        time.sleep(30)
        if datetime.now(ET).time() >= STREAM_STOP:
            log.info("EOD — stopping stream")
            try:
                wss.stop()
            except Exception:
                pass
            return


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now_et = datetime.now(ET)
    log.info(f"Stream process starting  {now_et.strftime('%H:%M %Z')}")

    if now_et.time() >= STREAM_STOP:
        log.info("Past market close — stream not needed")
        return

    # Write PID
    (STATE_DIR / "stream.pid").write_text(str(os.getpid()))

    api_key    = os.environ.get("ALPACA_API_KEY",    "").strip()
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        log.error("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        return

    try:
        from alpaca.data.live import StockDataStream
    except ImportError:
        log.error("alpaca-py not installed (alpaca.data.live)")
        return

    wss = StockDataStream(api_key, secret_key)
    wss.subscribe_bars(_handle_bar, *WATCHLIST)

    log.info(f"Subscribed to real-time bars: {', '.join(WATCHLIST)}")

    threading.Thread(target=_eod_watchdog, args=(wss,), daemon=True).start()

    try:
        wss.run()   # blocks until wss.stop() is called
    except Exception as e:
        log.error(f"Stream error: {e}")
    finally:
        pid_file = STATE_DIR / "stream.pid"
        if pid_file.exists():
            pid_file.unlink()
        log.info("Stream process exited")


if __name__ == "__main__":
    main()
