"""
data_client.py — Alpaca real-time and historical data layer.

Replaces yfinance for intraday data — zero delay, real-time bars.
Uses the same ALPACA_API_KEY / ALPACA_SECRET_KEY already in the environment.
The same alpaca-py package handles both execution AND data (no extra deps).
"""

import os
from datetime import datetime, timedelta, date
from typing import List, Optional, Dict

import pytz

ET = pytz.timezone("America/New_York")

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


# ── Client ────────────────────────────────────────────────────────────────────

def _get_data_client() -> Optional["StockHistoricalDataClient"]:
    if not ALPACA_DATA_AVAILABLE:
        return None
    api_key    = os.environ.get("ALPACA_API_KEY",    "").strip()
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        return None
    return StockHistoricalDataClient(api_key, secret_key)


# ── Bar → Candle conversion ───────────────────────────────────────────────────

def _bar_to_candle(bar) -> Candle:
    """Convert an Alpaca bar object to a naive-ET Candle."""
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


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_daily_bars(
    symbols: List[str],
    lookback_days: int = 30,
    end_date: Optional[date] = None,
) -> Dict[str, List[Candle]]:
    """
    Fetch daily bars for a list of stock symbols.
    Futures (ending in =F) are silently skipped.
    Returns {symbol: [Candle, ...]} sorted oldest-first.
    """
    client = _get_data_client()
    if not client:
        raise RuntimeError("Alpaca data client not configured — check ALPACA_API_KEY / ALPACA_SECRET_KEY")

    stocks = [s for s in symbols if not s.upper().endswith("=F")]
    if not stocks:
        return {}

    now_et  = datetime.now(ET)
    end_dt  = ET.localize(datetime.combine(end_date or now_et.date(), datetime.max.time())) if end_date else now_et
    start_dt = end_dt - timedelta(days=lookback_days + 7)   # +7 buffer for weekends/holidays

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
    Fetch intraday bars for a single stock symbol.
    start/end can be naive (assumed ET) or timezone-aware.
    Returns list of Candles sorted oldest-first.
    """
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
    raw = client.get_stock_bars(request)
    bars = raw.get(symbol, [])
    return sorted([_bar_to_candle(b) for b in bars], key=lambda c: c.timestamp)


def fetch_current_price(symbol: str) -> Optional[float]:
    """Return the most recent close price for a symbol via Alpaca latest bar."""
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
