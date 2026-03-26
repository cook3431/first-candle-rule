"""
portfolio_backtest.py — Portfolio-level backtesting for the First Candle Rule.

Runs N stocks × N trading days under two exit methodologies:
  Mode A: Fixed TP/SL — set levels, walk away (pure FCR rules)
  Mode B: Fixed Dollar Trail — trail = initial risk per share, no fixed TP

Usage:
    python portfolio_backtest.py
    python portfolio_backtest.py --stocks AAPL,TSLA,NVDA --days 10 --risk 100
"""

import sys, os
from datetime import datetime, timedelta, date, time
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import SystemConfig, MarketConfig
from models import Candle, Direction, SignalType
from strategy import FirstCandleStrategy
from live_scanner import (
    get_market_config,
    row_to_candle,
    to_est_naive,
    get_first_candle_30min,
    EST,
)

DEFAULT_STOCKS = ["QQQ", "SPY", "AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

RISK_PER_TRADE = 100.0
TARGET_RR      = 3.0
ACTIVATE_AT_R  = 1.0
EOD_CLOSE_TIME = time(15, 55)


# ─── Result dataclasses ───────────────────────────────────────────────────────

@dataclass
class TradeResult:
    symbol:       str
    date:         date
    direction:    str
    entry:        float
    initial_stop: float
    take_profit:  Optional[float]
    exit_price:   float
    exit_reason:  str   # "TP", "SL", "TRAIL", "EOD"
    pnl:          float
    r_multiple:   float
    qty:          int
    mode:         str   # "A" or "B"


@dataclass
class DayResult:
    symbol: str
    date:   date
    mode_a: Optional[TradeResult] = None
    mode_b: Optional[TradeResult] = None


@dataclass
class PortfolioBacktestResult:
    stocks:         List[str]
    days:           int
    risk_per_trade: float
    start_date:     date
    end_date:       date
    mode_a:         dict
    mode_b:         dict
    day_results:    List[DayResult]
    generated_at:   str


# ─── Simulation functions ─────────────────────────────────────────────────────

def simulate_mode_a(
    entry: float, stop_loss: float, take_profit: float,
    direction: str, qty: int, candles_post: List[Candle],
) -> Tuple[float, str]:
    """Mode A: fixed TP/SL. SL wins when both hit same candle."""
    for candle in candles_post:
        if direction == "LONG":
            sl_hit = candle.low  <= stop_loss
            tp_hit = candle.high >= take_profit
        else:
            sl_hit = candle.high >= stop_loss
            tp_hit = candle.low  <= take_profit

        if sl_hit and tp_hit:
            return stop_loss, "SL"
        if sl_hit:
            return stop_loss, "SL"
        if tp_hit:
            return take_profit, "TP"
        if candle.timestamp.time() >= EOD_CLOSE_TIME:
            return candle.close, "EOD"

    if candles_post:
        return candles_post[-1].close, "EOD"
    return entry, "EOD"


def simulate_mode_b(
    entry: float, stop_loss: float, direction: str,
    qty: int, candles_post: List[Candle], activate_at_r: float = 1.0,
) -> Tuple[float, str]:
    """Mode B: fixed dollar trail. Trail amount = initial risk. Activates at 1R."""
    initial_risk    = abs(entry - stop_loss)
    if initial_risk == 0:
        return entry, "EOD"

    trail_amount    = initial_risk
    current_stop    = stop_loss
    high_water_mark = entry
    trail_active    = False

    for candle in candles_post:
        if direction == "LONG":
            if candle.high > high_water_mark:
                high_water_mark = candle.high
            r_profit = (high_water_mark - entry) / initial_risk
            if not trail_active and r_profit >= activate_at_r:
                trail_active = True
            if trail_active:
                new_stop = high_water_mark - trail_amount
                if new_stop > current_stop:
                    current_stop = new_stop
            if candle.low <= current_stop:
                return current_stop, "TRAIL" if trail_active else "SL"
        else:  # SHORT
            if candle.low < high_water_mark:
                high_water_mark = candle.low
            r_profit = (entry - high_water_mark) / initial_risk
            if not trail_active and r_profit >= activate_at_r:
                trail_active = True
            if trail_active:
                new_stop = high_water_mark + trail_amount
                if new_stop < current_stop:
                    current_stop = new_stop
            if candle.high >= current_stop:
                return current_stop, "TRAIL" if trail_active else "SL"

        if candle.timestamp.time() >= EOD_CLOSE_TIME:
            return candle.close, "EOD"

    if candles_post:
        return candles_post[-1].close, "EOD"
    return entry, "EOD"


def calc_pnl(entry: float, exit_price: float, direction: str, qty: int) -> float:
    if direction == "LONG":
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty


def calc_r_multiple(entry: float, stop: float, exit_price: float, direction: str) -> float:
    risk = abs(entry - stop)
    if risk == 0:
        return 0.0
    if direction == "LONG":
        return (exit_price - entry) / risk
    return (entry - exit_price) / risk


# ─── Data helpers ─────────────────────────────────────────────────────────────

def fetch_intraday_candles_for_date(
    symbol: str, trade_date: date, interval: str = "5m",
) -> List[Candle]:
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    start  = (datetime.combine(trade_date, time(0, 0)) - timedelta(days=1)).strftime("%Y-%m-%d")
    end    = (datetime.combine(trade_date, time(0, 0)) + timedelta(days=1)).strftime("%Y-%m-%d")
    hist   = ticker.history(start=start, end=end, interval=interval)
    candles = []
    for ts, row in hist.iterrows():
        dt = to_est_naive(ts)
        if dt.date() == trade_date:
            candles.append(row_to_candle(dt, row))
    return candles


def fetch_daily_candles_for_date(
    symbol: str, trade_date: date, lookback: int = 10,
) -> List[Candle]:
    import yfinance as yf
    start  = (datetime.combine(trade_date, time(0, 0)) - timedelta(days=lookback * 2)).strftime("%Y-%m-%d")
    end    = datetime.combine(trade_date, time(0, 0)).strftime("%Y-%m-%d")
    ticker = yf.Ticker(symbol)
    hist   = ticker.history(start=start, end=end, interval="1d")
    candles = []
    for ts, row in hist.iterrows():
        dt = to_est_naive(ts)
        if dt.date() < trade_date:
            candles.append(row_to_candle(dt, row))
    return candles[-lookback:]


def get_last_n_trading_days(n: int) -> List[date]:
    result = []
    cursor = date.today() - timedelta(days=1)
    while len(result) < n:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor -= timedelta(days=1)
    return result


# ─── Per-day backtest ─────────────────────────────────────────────────────────

def backtest_stock_day(symbol: str, trade_date: date) -> Optional[DayResult]:
    """Run both Mode A and Mode B for one stock on one day."""
    if trade_date.weekday() == 0:   # Monday
        return None

    market_cfg = get_market_config(symbol)
    config     = SystemConfig()
    config.market = market_cfg
    config.strategy.kill_zone_only = False   # enforce via timing, not hard-block
    config.strategy.skip_mondays   = True

    strategy = FirstCandleStrategy(config)
    strategy.reset_daily_state(datetime.combine(trade_date, time(9, 30)))

    daily_candles  = fetch_daily_candles_for_date(symbol, trade_date, lookback=10)
    intraday_30min = fetch_intraday_candles_for_date(symbol, trade_date, interval="30m")
    intraday_5min  = fetch_intraday_candles_for_date(symbol, trade_date, interval="5m")

    if not daily_candles or not intraday_30min or not intraday_5min:
        return None

    confirmed_daily = daily_candles[-5:]
    strategy.determine_bias(confirmed_daily)
    if strategy.bias == Direction.NONE:
        return None

    prev_day = confirmed_daily[-1]
    strategy.mark_liquidity_levels(prev_day_high=prev_day.high, prev_day_low=prev_day.low)
    strategy.set_session_open_price(prev_day.close)

    fc_candle = get_first_candle_30min(intraday_30min)
    if fc_candle is None:
        return None
    strategy.mark_first_candle(fc_candle)

    post_range = [c for c in intraday_5min if c.timestamp.time() >= time(10, 0)]
    if not post_range:
        return None

    signal = None
    entry_candle_index = 0
    for i in range(3, len(post_range) + 1):
        window       = post_range[:i]
        current_time = datetime.combine(trade_date, window[-1].timestamp.time())
        if current_time.time() >= time(15, 0):
            break
        sig = strategy.generate_signal(current_time, window)
        if sig.signal_type in (SignalType.ENTER_LONG, SignalType.ENTER_SHORT):
            signal             = sig
            entry_candle_index = i - 1
            break

    if signal is None or signal.entry_price is None:
        return None

    # Suppress D-grade signals
    if signal.confidence and signal.confidence.score < 40:
        return None

    risk_per_share = abs(signal.entry_price - signal.stop_loss)
    if risk_per_share <= 0:
        return None
    qty       = max(1, int(RISK_PER_TRADE / risk_per_share))
    direction = signal.direction.value

    candles_post_entry = post_range[entry_candle_index:]

    # Mode A
    take_profit_a = signal.take_profit or (
        signal.entry_price + (TARGET_RR * risk_per_share)
        if direction == "LONG"
        else signal.entry_price - (TARGET_RR * risk_per_share)
    )
    exit_a, reason_a = simulate_mode_a(
        signal.entry_price, signal.stop_loss, take_profit_a,
        direction, qty, candles_post_entry
    )
    result_a = TradeResult(
        symbol=symbol, date=trade_date, direction=direction,
        entry=signal.entry_price, initial_stop=signal.stop_loss,
        take_profit=take_profit_a, exit_price=exit_a, exit_reason=reason_a,
        pnl=round(calc_pnl(signal.entry_price, exit_a, direction, qty), 2),
        r_multiple=round(calc_r_multiple(signal.entry_price, signal.stop_loss, exit_a, direction), 2),
        qty=qty, mode="A",
    )

    # Mode B
    exit_b, reason_b = simulate_mode_b(
        signal.entry_price, signal.stop_loss,
        direction, qty, candles_post_entry, ACTIVATE_AT_R
    )
    result_b = TradeResult(
        symbol=symbol, date=trade_date, direction=direction,
        entry=signal.entry_price, initial_stop=signal.stop_loss,
        take_profit=None, exit_price=exit_b, exit_reason=reason_b,
        pnl=round(calc_pnl(signal.entry_price, exit_b, direction, qty), 2),
        r_multiple=round(calc_r_multiple(signal.entry_price, signal.stop_loss, exit_b, direction), 2),
        qty=qty, mode="B",
    )

    return DayResult(symbol=symbol, date=trade_date, mode_a=result_a, mode_b=result_b)


# ─── Aggregation ──────────────────────────────────────────────────────────────

def aggregate_stats(trades: List[TradeResult], mode: str) -> dict:
    if not trades:
        return {"mode": mode, "total_trades": 0, "message": "No signals generated"}

    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl   = sum(t.pnl for t in trades)
    avg_win     = sum(t.pnl for t in wins)   / len(wins)   if wins   else 0
    avg_loss    = sum(t.pnl for t in losses) / len(losses) if losses else 0
    avg_r       = sum(t.r_multiple for t in trades) / len(trades)
    best_trade  = max(trades, key=lambda t: t.pnl)
    worst_trade = min(trades, key=lambda t: t.pnl)
    exit_reasons = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    return {
        "mode":           mode,
        "total_trades":   len(trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(len(wins) / len(trades) * 100, 1),
        "total_pnl":      round(total_pnl, 2),
        "avg_win":        round(avg_win, 2),
        "avg_loss":       round(avg_loss, 2),
        "avg_r_multiple": round(avg_r, 2),
        "best_trade":     {"symbol": best_trade.symbol,  "date": str(best_trade.date),
                           "pnl":    best_trade.pnl,     "r":    best_trade.r_multiple},
        "worst_trade":    {"symbol": worst_trade.symbol, "date": str(worst_trade.date),
                           "pnl":    worst_trade.pnl,    "r":    worst_trade.r_multiple},
        "exit_breakdown": exit_reasons,
        "max_single_win":  round(max(t.pnl for t in trades), 2),
        "max_single_loss": round(min(t.pnl for t in trades), 2),
        "expectancy":      round(total_pnl / len(trades), 2),
    }


# ─── Portfolio runner ─────────────────────────────────────────────────────────

def run_portfolio_backtest(
    stocks: List[str] = None,
    days:   int       = 10,
    risk:   float     = 100.0,
) -> PortfolioBacktestResult:
    global RISK_PER_TRADE
    RISK_PER_TRADE = risk

    if stocks is None:
        stocks = DEFAULT_STOCKS

    trading_days      = get_last_n_trading_days(days)
    all_day_results: List[DayResult] = []

    for symbol in stocks:
        for trade_date in trading_days:
            try:
                result = backtest_stock_day(symbol, trade_date)
                if result is not None:
                    all_day_results.append(result)
            except Exception as e:
                print(f"  ⚠ {symbol} {trade_date}: {e}")
                continue

    mode_a_stats = aggregate_stats([r.mode_a for r in all_day_results if r.mode_a], "A")
    mode_b_stats = aggregate_stats([r.mode_b for r in all_day_results if r.mode_b], "B")

    return PortfolioBacktestResult(
        stocks=stocks,
        days=days,
        risk_per_trade=risk,
        start_date=trading_days[-1] if trading_days else date.today(),
        end_date=trading_days[0]    if trading_days else date.today(),
        mode_a=mode_a_stats,
        mode_b=mode_b_stats,
        day_results=all_day_results,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Portfolio Backtest — Mode A vs Mode B")
    parser.add_argument("--stocks", default="", help="Comma-separated tickers (default: 10 built-in)")
    parser.add_argument("--days",   type=int,   default=10,  help="Trading days to backtest (default: 10)")
    parser.add_argument("--risk",   type=float, default=100, help="Dollar risk per trade (default: $100)")
    args = parser.parse_args()

    stocks = [s.strip().upper() for s in args.stocks.split(",") if s.strip()] or None
    print(f"\nRunning portfolio backtest: {args.days} days × {len(stocks or DEFAULT_STOCKS)} stocks @ ${args.risk} risk\n")
    result = run_portfolio_backtest(stocks=stocks, days=args.days, risk=args.risk)

    print(f"\n{'='*60}")
    print(f"PORTFOLIO BACKTEST RESULTS  ({result.start_date} → {result.end_date})")
    print(f"{'='*60}")
    for mode_stats in [result.mode_a, result.mode_b]:
        mode = mode_stats.get("mode", "?")
        label = "Mode A — Fixed TP/SL" if mode == "A" else "Mode B — Fixed Dollar Trail"
        print(f"\n{label}")
        if mode_stats.get("total_trades", 0) == 0:
            print("  No signals generated")
            continue
        print(f"  Trades:     {mode_stats['total_trades']}   "
              f"({mode_stats['wins']}W / {mode_stats['losses']}L — {mode_stats['win_rate']}% win rate)")
        print(f"  Total P&L:  ${mode_stats['total_pnl']:+.2f}   "
              f"Avg R: {mode_stats['avg_r_multiple']:.2f}   "
              f"Expectancy: ${mode_stats['expectancy']:+.2f}/trade")
        print(f"  Best:  {mode_stats['best_trade']['symbol']} ${mode_stats['best_trade']['pnl']:+.2f} "
              f"({mode_stats['best_trade']['r']:.1f}R)")
        print(f"  Worst: {mode_stats['worst_trade']['symbol']} ${mode_stats['worst_trade']['pnl']:+.2f} "
              f"({mode_stats['worst_trade']['r']:.1f}R)")
        print(f"  Exits: {mode_stats['exit_breakdown']}")
    print()
