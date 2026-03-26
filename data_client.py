"""
data_client.py — Alpaca data layer.

Free-tier Alpaca has a 15-minute delay on REST API calls.
Only the WebSocket stream (fcr_stream.py) is real-time on the free tier.

Strategy:
  - fetch_current_price()  → reads state/latest-prices.json (stream) first,
                             falls back to REST only if stream file is stale/missing
  - fetch_intraday_bars()  → reads state/stream-bars.json (stream) first,
                             falls back to REST for historical/morning calls
  - fetch_daily_bars()     → always REST (daily bars, no delay issue)
  - fetch_first_candle()   → always REST (called once at 10:01, bar already closed)
"""

import os, json
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import List, Optional, Dict

import pytz

ET       = pytz.timezone("America/New_York")
ROOT_DIR = Path(__file__).parent
STATE_DIR = ROOT_DIR / "state"

# Max age for stream data to be considered fresh (seconds)
STREAM_PRICE_MAX_AGE = 300   # 5 minutes
STREAM_BARS_MAX_AGE  = 600   # 10 minutes

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    ALPACA_DATA_AVAILABLE = True
except ImportError:
    ALPACA_DATA_AVAILABLE = False

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from models import Candle


# ── REST client ───────────────────────────────────────────────────────────────

def _get_data_client() -> Optional["StockHistoricalDataClient"]:
    if not ALPACA_DATA_AVAILABLE:
        return None
    api_key    = os.environ.get("ALPACA_API_KEY",    "").strip()
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        return None
    return StockHistoricalDataClient(api_key, secret_key)


def _bar_to_candle(bar) -> Candle:
    ts = bar.timestamp
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if ts.tzinfo is not None:
        ts = ts.astimezone(ET).replace(tzinfo=None)
    return Candle(
        timestamp=ts,
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
        volume=float(bar.volume) if bar.volume else 0.0,
    )


# ── Stream file readers ───────────────────────────────────────────────────────

def _stream_price(symbol: str) -> Optional[float]:
    """Read latest price from stream file. Returns None if stale or missing."""
    try:
        data = json.loads((STATE_DIR / "latest-prices.json").read_text())
        entry = data.get(symbol)
        if not entry:
            return None
        ts = datetime.fromisoformat(entry["timestamp"])
        age = (datetime.now() - ts).total_seconds()
        if age <= STREAM_PRICE_MAX_AGE:
            return float(entry["price"])
    except Exception:
        pass
    return None


def _stream_bars(symbol: str, start: datetime) -> Optional[List[Candle]]:
    """
    Read 5-min bars from stream file for a symbol, filtered to >= start.
    Returns None if file missing or data is stale.
    """
    try:
        path = STATE_DIR / "stream-bars.json"
        if not path.exists():
            return None
        age = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds()
        if age > STREAM_BARS_MAX_AGE:
            return None
        data  = json.loads(path.read_text())
        raw   = data.get(symbol, [])
        if not raw:
            return None
        candles = []
        for b in raw:
            ts = datetime.fromisoformat(b["timestamp"])
            if ts >= start:
                candles.append(Candle(
                    timestamp=ts,
                    open=float(b["open"]),
                    high=float(b["high"]),
                    low=float(b["low"]),
                    close=float(b["close"]),
                    volume=float(b.get("volume", 0)),
                ))
        return sorted(candles, key=lambda c: c.timestamp) if candles else None
    except Exception:
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_daily_bars(
    symbols: List[str],
    lookback_days: int = 30,
    end_date: Optional[date] = None,
) -> Dict[str, List[Candle]]:
    """Fetch daily bars via REST. No delay issue — daily bars are always historical."""
    client = _get_data_client()
    if not client:
        raise RuntimeError("Alpaca data client not configured — check ALPACA_API_KEY / ALPACA_SECRET_KEY")

    stocks = [s for s in symbols if not s.upper().endswith("=F")]
    if not stocks:
        return {}

    now_et   = datetime.now(ET)
    end_dt   = ET.localize(datetime.combine(end_date or now_et.date(), datetime.max.time())) if end_date else now_et
    start_dt = end_dt - timedelta(days=lookback_days + 7)

    request = StockBarsRequest(
        symbol_or_symbols=stocks,
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=start_dt.strftime("%Y-%m-%dT00:00:00"),
        end=end_dt.strftime("%Y-%m-%dT23:59:59"),
        feed="iex",
    )
    raw = client.get_stock_bars(request)
    result: Dict[str, List[Candle]] = {}
    for sym in stocks:
        bars = raw.get(sym, [])
        result[sym] = sorted([_bar_to_candle(b) for b in bars], key=lambda c: c.timestamp)
    return result


def fetch_intraday_bars(
    symbol: str,
    timeframe_minutes: int,
    start: datetime,
    end: Optional[datetime] = None,
) -> List[Candle]:
    """
    Fetch intraday 5-min bars.

    Primary: reads from state/stream-bars.json (real-time, written by fcr_stream.py).
    Fallback: REST API (used for first-candle fetch and morning calls where the
              stream file doesn't exist yet — those bars are already closed so
              the 15-min REST delay does not apply).
    """
    start_naive = start.replace(tzinfo=None) if start.tzinfo else start

    # Try stream file first (real-time, no delay)
    if timeframe_minutes == 5:
        stream_result = _stream_bars(symbol, start_naive)
        if stream_result is not None:
            return stream_result

    # Fall back to REST (acceptable for already-closed historical bars)
    client = _get_data_client()
    if not client:
        raise RuntimeError("Alpaca data client not configured")

    if start.tzinfo is None:
        start = ET.localize(start)
    end = end or datetime.now(ET)
    if end.tzinfo is None:
        end = ET.localize(end)

    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
        start=start.isoformat(),
        end=end.isoformat(),
        feed="iex",
    )
    raw  = client.get_stock_bars(request)
    bars = raw.get(symbol, [])
    return sorted([_bar_to_candle(b) for b in bars], key=lambda c: c.timestamp)


def fetch_current_price(symbol: str) -> Optional[float]:
    """
    Return the most recent price.

    Primary: reads from state/latest-prices.json (written by fcr_stream.py WebSocket).
    Fallback: REST latest-bar call (used when stream not running, e.g. pre-market).
    """
    # Try stream first (real-time)
    price = _stream_price(symbol)
    if price is not None:
        return price

    # Fall back to REST
    client = _get_data_client()
    if not client:
        return None
    try:
        request = StockLatestBarRequest(symbol_or_symbols=symbol, feed="iex")
        bar_map = client.get_stock_latest_bar(request)
        if bar_map and symbol in bar_map:
            return float(bar_map[symbol].close)
    except Exception:
        pass
    return None
