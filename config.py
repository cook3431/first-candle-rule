"""
First Candle Rule — Configuration
Based on Casper SMC (Jesse Rogers) strategy
"""

from dataclasses import dataclass, field
from typing import List, Optional
from datetime import time


@dataclass
class SessionConfig:
    """Trading session times (all in EST)"""
    # Cycle reset
    cycle_reset: time = time(18, 0)

    # Sessions
    asia_start: time = time(18, 0)
    asia_end: time = time(0, 0)

    london_start: time = time(0, 0)
    london_end: time = time(6, 0)
    london_kill_zone_start: time = time(3, 0)
    london_kill_zone_end: time = time(4, 0)

    pre_market_start: time = time(6, 0)
    pre_market_end: time = time(7, 30)

    session_open_mark: time = time(7, 30)

    ny_open: time = time(9, 30)

    ny_am_kill_zone_start: time = time(10, 0)
    ny_am_kill_zone_end: time = time(11, 0)

    ny_pm_kill_zone_start: time = time(14, 0)
    ny_pm_kill_zone_end: time = time(15, 0)


@dataclass
class FirstCandleConfig:
    """First Candle Rule parameters"""

    # --- Range Definition ---
    # Which version to use: "30min", "15min", "5min"
    range_timeframe: str = "30min"

    # Range candle duration mapping (minutes)
    range_durations = {
        "30min": 30,
        "15min": 15,
        "5min": 5,
    }

    # --- Entry Timeframe ---
    # LTF for watching displacement and FVGs
    # "5min" for 30min/15min range, "1min" for 5min range
    entry_timeframe: str = "5min"

    entry_durations = {
        "5min": 5,
        "1min": 1,
    }

    # --- FVG Settings ---
    # Minimum FVG size in ticks (filters out tiny gaps)
    min_fvg_ticks: int = 2

    # Where to enter within the FVG: "top", "middle", "bottom"
    fvg_entry_level: str = "top"  # top of FVG for shorts, bottom for longs

    # --- Stop Loss ---
    # "wick" = at the extreme of first candle (conservative)
    # "body" = at the body of first candle (standard)
    # "structure" = at nearby structure (aggressive)
    stop_loss_method: str = "wick"

    # Additional buffer beyond stop loss (in ticks)
    stop_loss_buffer_ticks: int = 2

    # --- Take Profit ---
    # Minimum risk-to-reward ratio
    min_risk_reward: float = 2.0

    # Optimal R:R (used if no obvious liquidity target)
    target_risk_reward: float = 3.0

    # Use liquidity levels as targets instead of fixed R:R
    use_liquidity_targets: bool = True

    # --- Risk Management ---
    risk_per_trade_pct: float = 1.0  # % of account
    max_losses_per_day: int = 2
    max_wins_per_day: int = 1
    max_trades_per_day: int = 3

    # --- Time Filters ---
    # Only trade during kill zones
    kill_zone_only: bool = True

    # Skip Mondays (first of weekly cycle)
    skip_mondays: bool = True

    # Stop trading after this time EST
    stop_trading_time: time = time(15, 0)

    # Close all trades by this time EST
    close_all_time: time = time(15, 55)


@dataclass
class MarketConfig:
    """Market-specific configuration"""
    symbol: str = "NQ"  # NQ, ES, etc.
    tick_size: float = 0.25  # NQ tick size
    tick_value: float = 5.0  # NQ dollar value per tick
    point_value: float = 20.0  # NQ dollar value per point


@dataclass
class PaperTradingConfig:
    """Paper trading account settings"""
    starting_balance: float = 100_000.0
    commission_per_side: float = 2.50  # per contract per side
    slippage_ticks: int = 1


@dataclass
class SystemConfig:
    """Master configuration"""
    session: SessionConfig = field(default_factory=SessionConfig)
    strategy: FirstCandleConfig = field(default_factory=FirstCandleConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    paper: PaperTradingConfig = field(default_factory=PaperTradingConfig)

    # Logging
    log_level: str = "INFO"
    log_file: str = "trading_log.json"

    # Data source (for future live integration)
    data_source: str = "simulated"  # "simulated", "alpaca", "tradovate", etc.
