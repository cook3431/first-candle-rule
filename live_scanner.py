#!/usr/bin/env python3
"""
First Candle Rule — Live Signal Scanner
========================================
Tells you EXACTLY when to buy or sell any stock, ETF, or futures contract
using the Casper SMC First Candle Rule strategy.

What it does:
  1. Fetches real market data from Yahoo Finance
  2. Determines directional bias from the daily chart
  3. Marks the first 30-min candle range (09:30–10:00 EST)
  4. Detects displacement breaks and Fair Value Gaps on 5-min chart
  5. Outputs a clear, actionable BUY / SELL / WAIT signal

Usage:
    python live_scanner.py AAPL             # Apple stock
    python live_scanner.py QQQ              # Nasdaq ETF (closest to NQ)
    python live_scanner.py SPY              # S&P 500 ETF
    python live_scanner.py NQ=F             # NQ futures
    python live_scanner.py TSLA --verbose   # Show candle-level detail
    python live_scanner.py QQQ --watch      # Re-check every 5 minutes

Note:
    yfinance has a ~15-minute delay on intraday data for free users.
    For live signals, run this at 10:15 EST or later to ensure the
    first candle (09:30–10:00) is available in the data feed.

Requirements:
    pip install -r requirements.txt
"""

import sys
import os
import time as time_module
from datetime import datetime, time, timedelta
from typing import List, Optional, Tuple
from enum import Enum

# Add the module directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import SystemConfig, MarketConfig
from models import Candle, Direction, SignalType, TradeSignal
from strategy import FirstCandleStrategy

try:
    import yfinance as yf
    import pytz
except ImportError:
    print("\nERROR: Missing dependencies. Install with:")
    print("  pip install -r requirements.txt\n")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

EST    = pytz.timezone("America/New_York")
LONDON = pytz.timezone("Europe/London")

# Session times
NY_OPEN          = time(9, 30)
FIRST_CANDLE_END = time(10, 0)   # 30-min range closes here
AM_KZ_END        = time(11, 0)
PM_KZ_START      = time(14, 0)
PM_KZ_END        = time(15, 0)
STOP_TRADING     = time(15, 0)
CLOSE_ALL        = time(15, 55)

# Futures configs — everything else defaults to stock ($1/point)
FUTURES_CONFIGS = {
    "NQ=F":  dict(tick_size=0.25, tick_value=5.0,   point_value=20.0),
    "ES=F":  dict(tick_size=0.25, tick_value=12.5,  point_value=50.0),
    "MNQ=F": dict(tick_size=0.25, tick_value=0.50,  point_value=2.0),
    "MES=F": dict(tick_size=0.25, tick_value=1.25,  point_value=5.0),
    "RTY=F": dict(tick_size=0.10, tick_value=5.0,   point_value=50.0),
    "YM=F":  dict(tick_size=1.0,  tick_value=5.0,   point_value=5.0),
}


class MarketPhase(Enum):
    WEEKEND      = "Weekend — market closed"
    PRE_MARKET   = "PRE_MARKET"
    FIRST_CANDLE = "FIRST_CANDLE"
    AM_KILL_ZONE = "AM_KILL_ZONE"
    MID_SESSION  = "MID_SESSION"
    PM_KILL_ZONE = "PM_KILL_ZONE"
    DONE         = "DONE"


def phase_label(phase: "MarketPhase") -> str:
    """Return a human-readable phase label with London times."""
    labels = {
        MarketPhase.WEEKEND:      "Weekend — market closed",
        MarketPhase.PRE_MARKET:   f"Pre-market — awaiting NY open ({ny_to_london_str(9,30)} / 09:30 NY)",
        MarketPhase.FIRST_CANDLE: f"First candle forming — DO NOT TRADE ({ny_to_london_str(9,30)}–{ny_to_london_str(10,0)})",
        MarketPhase.AM_KILL_ZONE: f"AM Kill Zone ({ny_to_london_str(10,0)}–{ny_to_london_str(11,0)}) — primary window",
        MarketPhase.MID_SESSION:  f"Mid-session ({ny_to_london_str(11,0)}–{ny_to_london_str(14,0)}) — lower probability",
        MarketPhase.PM_KILL_ZONE: f"PM Kill Zone ({ny_to_london_str(14,0)}–{ny_to_london_str(15,0)}) — secondary window",
        MarketPhase.DONE:         f"Done for today — past {ny_to_london_str(15,0)}",
    }
    return labels.get(phase, phase.name)


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def get_market_config(symbol: str) -> MarketConfig:
    sym = symbol.upper()
    if sym in FUTURES_CONFIGS:
        cfg = FUTURES_CONFIGS[sym]
        return MarketConfig(
            symbol=sym,
            tick_size=cfg["tick_size"],
            tick_value=cfg["tick_value"],
            point_value=cfg["point_value"],
        )
    # Default: stocks/ETFs
    return MarketConfig(symbol=sym, tick_size=0.01, tick_value=0.01, point_value=1.0)


def now_est() -> datetime:
    return datetime.now(EST)


def now_london() -> datetime:
    return datetime.now(LONDON)


def ny_to_london_str(h: int, m: int = 0) -> str:
    """Return a London-time string for a given NY market hour:minute."""
    now_ny = now_est()
    ny_dt  = EST.localize(datetime.combine(now_ny.date(), time(h, m)))
    ld     = ny_dt.astimezone(LONDON)
    return ld.strftime("%H:%M %Z")  # e.g. "15:00 GMT" or "16:00 BST"


def get_market_phase(t: time) -> MarketPhase:
    if t < NY_OPEN:
        return MarketPhase.PRE_MARKET
    elif NY_OPEN <= t < FIRST_CANDLE_END:
        return MarketPhase.FIRST_CANDLE
    elif FIRST_CANDLE_END <= t <= AM_KZ_END:
        return MarketPhase.AM_KILL_ZONE
    elif AM_KZ_END < t < PM_KZ_START:
        return MarketPhase.MID_SESSION
    elif PM_KZ_START <= t <= PM_KZ_END:
        return MarketPhase.PM_KILL_ZONE
    else:
        return MarketPhase.DONE


def row_to_candle(ts: datetime, row) -> Candle:
    return Candle(
        timestamp=ts,
        open=float(row["Open"]),
        high=float(row["High"]),
        low=float(row["Low"]),
        close=float(row["Close"]),
        volume=float(row.get("Volume", 0) or 0),
    )


def to_est_naive(ts) -> datetime:
    """Convert any pandas Timestamp to a naive EST datetime."""
    if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
        ts = ts.tz_convert(EST)
    else:
        ts = pytz.utc.localize(ts).tz_convert(EST)
    return ts.replace(tzinfo=None)


# ─────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────

def fetch_daily_candles(symbol: str, days: int = 10) -> List[Candle]:
    """Fetch recent daily candles for bias + liquidity levels."""
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period=f"{days}d", interval="1d")
    candles = []
    for ts, row in hist.iterrows():
        dt = to_est_naive(ts)
        candles.append(row_to_candle(dt, row))
    return candles


def fetch_intraday_candles(symbol: str, interval: str) -> List[Candle]:
    """Fetch today's intraday candles."""
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="2d", interval=interval)

    today = now_est().date()
    candles = []
    for ts, row in hist.iterrows():
        dt = to_est_naive(ts)
        if dt.date() != today:
            continue
        candles.append(row_to_candle(dt, row))
    return candles


def get_first_candle_30min(intraday: List[Candle]) -> Optional[Candle]:
    """Find the 09:30 candle from a 30-min series."""
    for c in intraday:
        if c.timestamp.hour == 9 and c.timestamp.minute == 30:
            return c
    return None


def get_post_range_5min(intraday: List[Candle]) -> List[Candle]:
    """Get 5-min candles that opened at or after 10:00 EST."""
    return [c for c in intraday if c.timestamp.time() >= FIRST_CANDLE_END]


# ─────────────────────────────────────────────────────────────
# DISPLAY
# ─────────────────────────────────────────────────────────────

def box(title: str):
    print(f"\n  ┌{'─' * (len(title) + 2)}┐")
    print(f"  │ {title} │")
    print(f"  └{'─' * (len(title) + 2)}┘")


def arrow(direction: Direction) -> str:
    if direction == Direction.LONG:  return "▲"
    if direction == Direction.SHORT: return "▼"
    return "–"


def print_signal(
    symbol: str,
    signal: TradeSignal,
    fc_high: float,
    fc_low: float,
    market_cfg: MarketConfig,
    account_size: float = 100_000.0,
    risk_pct: float = 1.0,
):
    direction = signal.direction
    action    = "BUY  (LONG)" if direction == Direction.LONG else "SELL (SHORT)"
    a         = arrow(direction)

    risk_pts    = abs(signal.entry_price - signal.stop_loss)
    reward_pts  = abs(signal.take_profit - signal.entry_price)
    risk_dollars = risk_pts * market_cfg.point_value

    # Position sizing
    risk_budget = account_size * (risk_pct / 100)
    if risk_dollars > 0:
        if market_cfg.symbol in FUTURES_CONFIGS:
            units = max(1, int(risk_budget / risk_dollars))
            unit_label = "contract(s)"
        else:
            units = max(1, int(risk_budget / risk_pts))
            unit_label = "share(s)"
    else:
        units, unit_label = 1, "unit(s)"

    print(f"\n{'═' * 58}")
    print(f"  {a}  SIGNAL: {action}  {symbol.upper()}")
    print(f"{'═' * 58}")
    print(f"  ENTRY PRICE   →  {signal.entry_price:.2f}")
    print(f"  STOP LOSS     →  {signal.stop_loss:.2f}   "
          f"({'below first candle low' if direction == Direction.LONG else 'above first candle high'})")
    print(f"  TAKE PROFIT   →  {signal.take_profit:.2f}")
    print(f"{'─' * 58}")
    print(f"  Risk          :  {risk_pts:.2f} pts  (${risk_dollars:,.0f}/contract)")
    print(f"  Reward        :  {reward_pts:.2f} pts")
    print(f"  R:R ratio     :  {signal.risk_reward:.1f}:1")
    print(f"{'─' * 58}")
    print(f"  Position size : ~{units} {unit_label}  (1% risk on ${account_size:,.0f})")
    if signal.fvg:
        print(f"  FVG zone      :  {signal.fvg.bottom:.2f} – {signal.fvg.top:.2f}  "
              f"({signal.fvg.size:.2f} pts)")
    print(f"  First candle  :  H {fc_high:.2f}  /  L {fc_low:.2f}")
    print(f"{'─' * 58}")
    print(f"  HOW TO EXECUTE:")
    if direction == Direction.LONG:
        print(f"  1. Place LIMIT BUY at {signal.entry_price:.2f}")
        print(f"  2. Set STOP LOSS at   {signal.stop_loss:.2f}  (hard stop — no exceptions)")
        print(f"  3. Set TAKE PROFIT at {signal.take_profit:.2f}")
    else:
        print(f"  1. Place LIMIT SELL (short) at {signal.entry_price:.2f}")
        print(f"  2. Set STOP LOSS at           {signal.stop_loss:.2f}  (hard stop)")
        print(f"  3. Set TAKE PROFIT at         {signal.take_profit:.2f}")
    print(f"  4. Walk away. Let price do the work.")
    print(f"{'═' * 58}\n")


def print_no_signal(
    symbol: str,
    phase: MarketPhase,
    bias: Direction,
    fc_high: Optional[float],
    fc_low: Optional[float],
    current_price: Optional[float],
    reason: str,
):
    print(f"\n{'─' * 58}")
    print(f"  – NO TRADE  |  {symbol.upper()}  |  Bias: {arrow(bias)} {bias.value}")
    if fc_high and fc_low:
        print(f"  First candle range: {fc_low:.2f} – {fc_high:.2f}")
    if current_price and fc_high and fc_low:
        if current_price > fc_high:
            print(f"  Price {current_price:.2f} is ABOVE range → watching for bearish FVG retrace → SHORT")
        elif current_price < fc_low:
            print(f"  Price {current_price:.2f} is BELOW range → watching for bullish FVG retrace → LONG")
        else:
            print(f"  Price {current_price:.2f} is INSIDE range → waiting for displacement break")
    print(f"  Status: {reason}")
    print(f"  Phase:  {phase.value}")
    print(f"{'─' * 58}\n")


# ─────────────────────────────────────────────────────────────
# MAIN SCANNER
# ─────────────────────────────────────────────────────────────

def scan(symbol: str, verbose: bool = False, account_size: float = 100_000.0) -> dict:
    """
    Run the First Candle Rule scan on a symbol.
    Returns a dict with the full signal and all levels.
    """
    now = now_est()
    weekday = now.weekday()  # 0=Mon, 6=Sun
    t = now.time()

    now_ld = now.astimezone(LONDON)
    print(f"\n{'═' * 58}")
    print(f"  FIRST CANDLE RULE  |  {symbol.upper()}")
    print(f"  {now_ld.strftime('%A %Y-%m-%d  %H:%M:%S %Z')} ({now.strftime('%H:%M NY')})")
    print(f"{'═' * 58}")

    # ── WEEKEND ──
    if weekday >= 5:
        print(f"\n  Market is closed (weekend). Come back Monday.")
        return {"status": "CLOSED"}

    phase = get_market_phase(t)

    # ── DONE FOR DAY ──
    if phase == MarketPhase.DONE:
        print(f"\n  Session over. Run again tomorrow from {ny_to_london_str(9,45)} London time onward.")
        return {"status": "DONE"}

    # ── CONFIG ──
    market_cfg = get_market_config(symbol)
    config = SystemConfig()
    config.market = market_cfg
    config.strategy.kill_zone_only = False  # We enforce kill zones via phase display, not hard-block
    config.strategy.skip_mondays = True

    strategy = FirstCandleStrategy(config)
    strategy.reset_daily_state(now.replace(tzinfo=None))

    # ── MONDAY SKIP ──
    if weekday == 0:
        print(f"\n  SKIP — Monday is first of the weekly cycle (accumulation day).")
        print(f"  Rule: No trades on Mondays. Come back Tuesday.")
        return {"status": "NO_TRADE", "reason": "Monday"}

    # ─────────────────────────────────────────────
    # STEP 1: DAILY BIAS
    # ─────────────────────────────────────────────
    print(f"\n  [1/4] BIAS  ──────────────────────────────")

    daily_candles = fetch_daily_candles(symbol, days=12)

    if len(daily_candles) < 3:
        print("  ERROR: Not enough daily data. Check the symbol and try again.")
        return {"status": "ERROR", "reason": "Insufficient daily data"}

    # Use last 5 confirmed daily candles (exclude today's partial)
    confirmed_daily = [c for c in daily_candles if c.timestamp.date() < now.date()][-5:]
    if not confirmed_daily:
        confirmed_daily = daily_candles[-5:]

    bias = strategy.determine_bias(confirmed_daily)

    prev_day = confirmed_daily[-1]
    pdh = prev_day.high
    pdl = prev_day.low

    strategy.mark_liquidity_levels(prev_day_high=pdh, prev_day_low=pdl)
    strategy.set_session_open_price(prev_day.close)

    bias_str = f"{arrow(bias)} {bias.value}"
    print(f"  Bias:   {bias_str}")
    print(f"  PDH:    {pdh:.2f}  (previous day high — likely take profit target)")
    print(f"  PDL:    {pdl:.2f}  (previous day low  — likely take profit target)")

    if bias == Direction.NONE:
        print(f"\n  NO CLEAR BIAS — No trade today. Rule: no bias = no trade.")
        return {"status": "NO_TRADE", "reason": "No clear bias"}

    # ─────────────────────────────────────────────
    # STEP 2: FIRST CANDLE RANGE
    # ─────────────────────────────────────────────
    print(f"\n  [2/4] FIRST CANDLE RANGE  ────────────────")

    if phase == MarketPhase.PRE_MARKET:
        print(f"  Market opens at {ny_to_london_str(9,30)} ({ny_to_london_str(10,0)} London = first candle close).")
        print(f"  Come back at {ny_to_london_str(10,0)}–{ny_to_london_str(10,15)} London time (after the first candle closes).")
        print(f"  Bias is {bias_str} — prepare to watch for a {'bullish' if bias == Direction.LONG else 'bearish'} setup.")
        return {"status": "WAITING", "reason": "Pre-market", "bias": bias.value}

    intraday_30min = fetch_intraday_candles(symbol, "30m")
    first_candle_raw = get_first_candle_30min(intraday_30min)

    if phase == MarketPhase.FIRST_CANDLE:
        print(f"  First candle is still forming (closes at {ny_to_london_str(10,0)} London time).")
        print(f"  DO NOT TRADE during this period — just observe.")
        if first_candle_raw:
            print(f"  In progress: H={first_candle_raw.high:.2f}  L={first_candle_raw.low:.2f}")
        return {"status": "WAITING", "reason": "First candle still forming"}

    if first_candle_raw is None:
        print(f"  WARNING: First candle not found in data.")
        print(f"  yfinance may have a ~15-min delay. Try again after {ny_to_london_str(10,15)} London time.")
        return {"status": "WAITING", "reason": "First candle data not yet available (delay)"}

    fc = strategy.mark_first_candle(first_candle_raw)
    print(f"  HIGH:   {fc.high:.2f}")
    print(f"  LOW:    {fc.low:.2f}")
    print(f"  Range:  {fc.range_size:.2f} pts  ({'wide' if fc.range_size > 20 else 'narrow'} range)")

    # ─────────────────────────────────────────────
    # STEP 3: POST-RANGE CANDLE SCAN
    # ─────────────────────────────────────────────
    print(f"\n  [3/4] DISPLACEMENT + FVG SCAN  ───────────")

    intraday_5min = fetch_intraday_candles(symbol, "5m")
    post_range = get_post_range_5min(intraday_5min)

    if not post_range:
        print(f"  No post-range candles yet. Try again after {ny_to_london_str(10,10)} London time.")
        return {"status": "WAITING", "reason": "No post-range candles yet"}

    current_price = post_range[-1].close

    if verbose:
        print(f"  Post-range candles ({len(post_range)} total):")
        for c in post_range[-8:]:
            tag = ""
            if c.close > fc.high: tag = " ← ABOVE RANGE"
            elif c.close < fc.low: tag = " ← BELOW RANGE"
            print(f"    {c.timestamp.strftime('%H:%M')}  "
                  f"O={c.open:.2f}  H={c.high:.2f}  L={c.low:.2f}  C={c.close:.2f}{tag}")

    # Walk through candles looking for first valid signal
    signal: Optional[TradeSignal] = None
    candle_buffer: List[Candle] = []

    for candle in post_range:
        if candle.timestamp.time() >= STOP_TRADING:
            break
        candle_buffer.append(candle)

        if len(candle_buffer) < 3:
            continue

        sig = strategy.generate_signal(candle.timestamp, candle_buffer)

        if sig.signal_type in (SignalType.ENTER_LONG, SignalType.ENTER_SHORT):
            signal = sig
            break

    # Check if displacement has been detected (even without FVG yet)
    displacement_detected = strategy.range_broken_high or strategy.range_broken_low
    displacement_dir = strategy.displacement_direction

    if displacement_detected:
        dir_word = "upward" if displacement_dir == Direction.LONG else "downward"
        expected_trade = "SHORT (swept highs → distributing lower)" if displacement_dir == Direction.LONG \
                        else "LONG (swept lows → distributing higher)"
        print(f"  Displacement: {dir_word.upper()} break detected")
        print(f"  Expecting:    {expected_trade}")
    else:
        print(f"  No displacement break of range yet")
        print(f"  Watching: Break above {fc.high:.2f} or below {fc.low:.2f}")

    # ─────────────────────────────────────────────
    # STEP 4: OUTPUT SIGNAL
    # ─────────────────────────────────────────────
    print(f"\n  [4/4] SIGNAL  ────────────────────────────")

    if signal and signal.signal_type in (SignalType.ENTER_LONG, SignalType.ENTER_SHORT):
        if signal.timestamp:
            sig_ny = EST.localize(signal.timestamp) if signal.timestamp.tzinfo is None else signal.timestamp.astimezone(EST)
            sig_ld = sig_ny.astimezone(LONDON)
            sig_time = f"{sig_ld.strftime('%H:%M %Z')} ({sig_ny.strftime('%H:%M NY')})"
        else:
            sig_time = "–"
        print(f"  Signal generated at: {sig_time}")
        print_signal(symbol, signal, fc.high, fc.low, market_cfg, account_size)

        return {
            "status": "SIGNAL",
            "direction": signal.direction.value,
            "entry": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "risk_reward": round(signal.risk_reward, 2),
            "bias": bias.value,
            "phase": phase.name,
            "first_candle": {"high": fc.high, "low": fc.low},
            "fvg": {"top": signal.fvg.top, "bottom": signal.fvg.bottom} if signal.fvg else None,
        }

    else:
        if displacement_detected and not signal:
            reason = "Displacement detected — waiting for FVG to form"
        elif not displacement_detected:
            reason = "No displacement break yet — waiting"
        else:
            reason = "No valid setup (no aligned FVG in range)"

        print_no_signal(symbol, phase, bias, fc.high, fc.low, current_price, reason)

        return {
            "status": "NO_SIGNAL",
            "reason": reason,
            "bias": bias.value,
            "phase": phase.name,
            "displacement": displacement_detected,
            "first_candle": {"high": fc.high, "low": fc.low},
            "current_price": current_price,
        }


# ─────────────────────────────────────────────────────────────
# WATCH MODE
# ─────────────────────────────────────────────────────────────

def watch(symbol: str, interval_seconds: int = 300, verbose: bool = False):
    """
    Continuously scan and re-check on an interval.
    Stops automatically when a signal fires or when session ends.
    """
    print(f"\n  WATCH MODE — checking {symbol.upper()} every {interval_seconds // 60} min")
    print(f"  Press Ctrl+C to stop.\n")

    while True:
        result = scan(symbol, verbose=verbose)

        status = result.get("status", "")

        if status == "SIGNAL":
            print(f"  Signal fired — stopping watch.")
            break
        if status in ("DONE", "CLOSED"):
            print(f"  Session over — stopping watch.")
            break

        phase_name = result.get("phase", "")
        if phase_name == "DONE":
            break

        now = now_est()
        next_check_ld = now.astimezone(LONDON) + timedelta(seconds=interval_seconds)
        print(f"  Next check at {next_check_ld.strftime('%H:%M:%S %Z')} — sleeping...\n")

        try:
            time_module.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("\n  Watch stopped by user.")
            break


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    symbol      = "QQQ"   # Default: Nasdaq ETF (closest liquid proxy to NQ)
    verbose     = False
    watch_mode  = False
    account     = 100_000.0
    interval    = 300      # 5 min default watch interval

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--verbose", "-v"):
            verbose = True
        elif arg == "--watch":
            watch_mode = True
        elif arg == "--account" and i + 1 < len(args):
            account = float(args[i + 1])
            i += 1
        elif arg == "--interval" and i + 1 < len(args):
            interval = int(args[i + 1])
            i += 1
        elif not arg.startswith("--"):
            symbol = arg
        i += 1

    if watch_mode:
        watch(symbol, interval_seconds=interval, verbose=verbose)
    else:
        scan(symbol, verbose=verbose, account_size=account)


if __name__ == "__main__":
    main()
