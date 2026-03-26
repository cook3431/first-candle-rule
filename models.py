"""
First Candle Rule — Data Models
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List


class Direction(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class TradeStatus(Enum):
    PENDING = "PENDING"        # Limit order placed, waiting for fill
    ACTIVE = "ACTIVE"          # Trade is live
    CLOSED_WIN = "CLOSED_WIN"  # Closed at profit
    CLOSED_LOSS = "CLOSED_LOSS"  # Closed at loss
    CLOSED_BE = "CLOSED_BE"    # Closed at breakeven
    CANCELLED = "CANCELLED"    # Order cancelled (no fill)
    CLOSED_EOD = "CLOSED_EOD"  # Closed end of day


class SignalType(Enum):
    NO_SIGNAL = "NO_SIGNAL"
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    SKIP = "SKIP"


class NoTradeReason(Enum):
    NO_BIAS = "No clear directional bias identified"
    MONDAY = "Mondays are first of weekly cycle — skip"
    ASIA_SESSION = "Asia session — first of daily cycle"
    OUTSIDE_KILL_ZONE = "Outside kill zone hours"
    NO_DISPLACEMENT = "No displacement break of first candle range"
    NO_FVG = "No Fair Value Gap formed after displacement"
    DAILY_WIN_LIMIT = "Already hit daily win limit"
    DAILY_LOSS_LIMIT = "Already hit daily loss limit"
    DAILY_TRADE_LIMIT = "Already hit daily trade limit"
    PAST_STOP_TIME = "Past stop trading time"
    WEAK_BREAK = "Break was not impulsive — no displacement"
    FVG_TOO_SMALL = "FVG too small (below minimum tick threshold)"
    NO_LIQUIDITY_TARGET = "No obvious liquidity target for take profit"


class DayType(Enum):
    UNKNOWN  = "unknown"
    TRENDING = "trending"
    MIXUP    = "mixup"    # Both H and L broken — choppy
    STEADY   = "steady"   # No displacement yet


class ConfidenceGrade(Enum):
    A_PLUS = "A+"
    B      = "B"
    C      = "C"
    D      = "D"
    TRAP   = "TRAP"


@dataclass
class ConfidenceScore:
    grade:                ConfidenceGrade
    score:                int            # 0-100 composite
    fvg_quality:          int            # 0-90 sub-score from FVG checks
    displacement_quality: float          # 0.0-1.0
    bias_strength:        float          # 0.0-1.0
    in_kill_zone:         bool
    bias_aligned:         bool
    trap_fvg:             bool
    reasons:              List[str]      # Human-readable list
    recommendation:       str            # One-line plain English summary


@dataclass
class Candle:
    """Represents a single price candle"""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def total_range(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - self.body_high

    @property
    def lower_wick(self) -> float:
        return self.body_low - self.low


@dataclass
class FirstCandleRange:
    """The first candle's high and low — our trading range"""
    high: float
    low: float
    candle: Candle
    timestamp: datetime

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2

    @property
    def range_size(self) -> float:
        return self.high - self.low


@dataclass
class FairValueGap:
    """A Fair Value Gap (3-candle imbalance)"""
    top: float          # Top of the gap
    bottom: float       # Bottom of the gap
    direction: Direction  # Direction of the displacement that created it
    candle_1: Candle    # Candle before displacement
    candle_2: Candle    # Displacement candle
    candle_3: Candle    # Candle after displacement
    timestamp: datetime

    @property
    def size(self) -> float:
        return self.top - self.bottom

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class LiquidityLevel:
    """A time-based liquidity level"""
    price: float
    label: str  # e.g., "PDH", "PDL", "PWH", "PWL", "Asia High", etc.
    timestamp: datetime


@dataclass
class TradeSignal:
    """Output of the strategy engine — trade or no trade"""
    signal_type: SignalType
    direction: Direction = Direction.NONE
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward: Optional[float] = None
    fvg: Optional[FairValueGap] = None
    first_candle: Optional[FirstCandleRange] = None
    no_trade_reasons: List[NoTradeReason] = field(default_factory=list)
    timestamp: Optional[datetime] = None
    notes: str = ""
    confidence: Optional['ConfidenceScore'] = None

    @property
    def risk_amount(self) -> Optional[float]:
        if self.entry_price and self.stop_loss:
            return abs(self.entry_price - self.stop_loss)
        return None


@dataclass
class Trade:
    """A complete trade record"""
    id: str
    signal: TradeSignal
    status: TradeStatus = TradeStatus.PENDING
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    entry_price: float = 0.0
    exit_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    direction: Direction = Direction.NONE
    contracts: int = 1
    pnl: float = 0.0
    pnl_ticks: float = 0.0
    commission: float = 0.0
    notes: str = ""


@dataclass
class DailyStats:
    """Daily trading statistics"""
    date: datetime
    trades: List[Trade] = field(default_factory=list)
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    is_done_for_day: bool = False
    done_reason: str = ""

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades * 100
