"""
First Candle Rule — Strategy Engine
Based on Casper SMC (Jesse Rogers)

This is the core decision engine. It takes market data and outputs
trade signals based on the First Candle Rule playbook.
"""

from datetime import datetime, time, timedelta
from typing import List, Optional, Tuple
from models import (
    Candle, FirstCandleRange, FairValueGap, LiquidityLevel,
    TradeSignal, SignalType, Direction, NoTradeReason, DailyStats,
    ConfidenceScore, ConfidenceGrade, DayType,
)
from config import SystemConfig


class FirstCandleStrategy:
    """
    Core strategy engine for the First Candle Rule.

    Lifecycle per trading day:
    1. Pre-market: Mark liquidity levels, establish bias
    2. 09:30: First candle starts — mark high/low when it closes
    3. Post-close: Watch for displacement break of range
    4. On displacement: Identify FVGs back into range
    5. On FVG: Generate entry signal with SL/TP
    6. Manage trade until exit or EOD
    """

    def __init__(self, config: SystemConfig):
        self.config = config
        self.sc = config.strategy
        self.session = config.session
        self.market = config.market

        # Daily state
        self.first_candle: Optional[FirstCandleRange] = None
        self.bias: Direction = Direction.NONE
        self.liquidity_levels: List[LiquidityLevel] = []
        self.session_open_price: Optional[float] = None
        self.daily_stats: DailyStats = DailyStats(date=datetime.now())

        # State tracking
        self.range_broken_high: bool = False
        self.range_broken_low: bool = False
        self.displacement_direction: Direction = Direction.NONE
        self.active_fvgs: List[FairValueGap] = []
        self.signal_generated: bool = False

        # Candle buffer for FVG detection
        self._candle_buffer: List[Candle] = []

        # Confidence scoring state
        self._bias_strength: float = 0.5
        self._displacement_quality: float = 0.5

    # ──────────────────────────────────────────────
    # RESET / INITIALIZATION
    # ──────────────────────────────────────────────

    def reset_daily_state(self, date: datetime):
        """Reset all state for a new trading day"""
        self.first_candle = None
        self.bias = Direction.NONE
        self.liquidity_levels = []
        self.session_open_price = None
        self.daily_stats = DailyStats(date=date)
        self.range_broken_high = False
        self.range_broken_low = False
        self.displacement_direction = Direction.NONE
        self.active_fvgs = []
        self.signal_generated = False
        self._candle_buffer = []
        self._bias_strength = 0.5
        self._displacement_quality = 0.5

    # ──────────────────────────────────────────────
    # STEP 1: BIAS DETERMINATION
    # ──────────────────────────────────────────────

    def determine_bias(self, daily_candles: List[Candle]) -> Direction:
        """
        Determine directional bias from higher timeframe.
        Also calculates and stores self._bias_strength (0.0-1.0).
        """
        self._bias_strength = 0.5  # default moderate

        if len(daily_candles) < 3:
            self.bias = Direction.NONE
            self._bias_strength = 0.0
            return self.bias

        recent = daily_candles[-3:]

        bullish_displacement = any(
            c.is_bullish and c.body_size > c.total_range * 0.6 for c in recent
        )
        bearish_displacement = any(
            c.is_bearish and c.body_size > c.total_range * 0.6 for c in recent
        )

        closes_rising  = recent[-1].close > recent[0].close
        closes_falling = recent[-1].close < recent[0].close
        bullish_count  = sum(1 for c in recent if c.is_bullish)
        bearish_count  = sum(1 for c in recent if c.is_bearish)

        if bullish_displacement and closes_rising:
            self.bias = Direction.LONG
            self._bias_strength = 0.9 if bullish_count >= 3 else (0.75 if bullish_count >= 2 else 0.6)
        elif bearish_displacement and closes_falling:
            self.bias = Direction.SHORT
            self._bias_strength = 0.9 if bearish_count >= 3 else (0.75 if bearish_count >= 2 else 0.6)
        elif closes_rising:
            self.bias = Direction.LONG
            self._bias_strength = 0.45 if bullish_count >= 2 else 0.25
        elif closes_falling:
            self.bias = Direction.SHORT
            self._bias_strength = 0.45 if bearish_count >= 2 else 0.25
        else:
            self.bias = Direction.NONE
            self._bias_strength = 0.0
            return self.bias

        # Session open price confirmation/contradiction
        if self.session_open_price is not None:
            current_price = recent[-1].close
            if self.bias == Direction.LONG and current_price > self.session_open_price:
                self._bias_strength = min(1.0, self._bias_strength + 0.1)
            elif self.bias == Direction.SHORT and current_price < self.session_open_price:
                self._bias_strength = min(1.0, self._bias_strength + 0.1)
            else:
                self._bias_strength = max(0.0, self._bias_strength - 0.1)

        return self.bias

    # ──────────────────────────────────────────────
    # STEP 2: LIQUIDITY LEVELS
    # ──────────────────────────────────────────────

    def mark_liquidity_levels(
        self,
        prev_day_high: float,
        prev_day_low: float,
        prev_week_high: Optional[float] = None,
        prev_week_low: Optional[float] = None,
        asia_high: Optional[float] = None,
        asia_low: Optional[float] = None,
        london_high: Optional[float] = None,
        london_low: Optional[float] = None,
    ):
        """Mark all time-based liquidity levels for the day"""
        now = datetime.now()
        self.liquidity_levels = []

        levels = [
            (prev_day_high, "PDH"),
            (prev_day_low, "PDL"),
            (prev_week_high, "PWH"),
            (prev_week_low, "PWL"),
            (asia_high, "Asia High"),
            (asia_low, "Asia Low"),
            (london_high, "London High"),
            (london_low, "London Low"),
        ]

        for price, label in levels:
            if price is not None:
                self.liquidity_levels.append(
                    LiquidityLevel(price=price, label=label, timestamp=now)
                )

    def set_session_open_price(self, price: float):
        """Mark the 07:30 EST session open price"""
        self.session_open_price = price

    # ──────────────────────────────────────────────
    # STEP 3: FIRST CANDLE RANGE
    # ──────────────────────────────────────────────

    def mark_first_candle(self, candle: Candle) -> FirstCandleRange:
        """
        Mark the high and low of the first candle after NY open.
        This defines our trading range for the day.
        """
        self.first_candle = FirstCandleRange(
            high=candle.high,
            low=candle.low,
            candle=candle,
            timestamp=candle.timestamp,
        )
        return self.first_candle

    # ──────────────────────────────────────────────
    # STEP 4: DISPLACEMENT DETECTION
    # ──────────────────────────────────────────────

    def check_displacement_break(self, candle: Candle) -> Tuple[bool, Direction]:
        """
        Check if a candle has broken the first candle range with displacement.
        Stores displacement quality (0.0-1.0) in self._displacement_quality as side effect.
        Minimum body ratio raised to 0.5 per playbook (impulsive move requirement).
        """
        if self.first_candle is None:
            return False, Direction.NONE

        fc = self.first_candle
        min_ratio = self.sc.min_displacement_body_ratio  # 0.5

        if candle.close > fc.high:
            if candle.total_range > 0:
                body_ratio = candle.body_size / candle.total_range
                is_strong  = body_ratio >= min_ratio
                quality    = min(body_ratio / 0.8, 1.0)
            else:
                is_strong = False
                quality   = 0.0

            if is_strong:
                self.range_broken_high        = True
                self.displacement_direction   = Direction.LONG
                self._displacement_quality    = quality
                return True, Direction.LONG

        if candle.close < fc.low:
            if candle.total_range > 0:
                body_ratio = candle.body_size / candle.total_range
                is_strong  = body_ratio >= min_ratio
                quality    = min(body_ratio / 0.8, 1.0)
            else:
                is_strong = False
                quality   = 0.0

            if is_strong:
                self.range_broken_low       = True
                self.displacement_direction = Direction.SHORT
                self._displacement_quality  = quality
                return True, Direction.SHORT

        return False, Direction.NONE

    # ──────────────────────────────────────────────
    # STEP 5: FVG DETECTION
    # ──────────────────────────────────────────────

    def detect_fvg(self, candles: List[Candle]) -> Optional[FairValueGap]:
        """
        Detect a Fair Value Gap from a sequence of 3 candles.

        FVG (Bullish):
            - Candle 1 high < Candle 3 low = gap between them
            - Candle 2 is the large impulsive candle

        FVG (Bearish):
            - Candle 1 low > Candle 3 high = gap between them
            - Candle 2 is the large impulsive candle
        """
        if len(candles) < 3:
            return None

        c1, c2, c3 = candles[-3], candles[-2], candles[-1]

        # Bullish FVG: gap between c1.high and c3.low
        if c3.low > c1.high:
            gap_size = c3.low - c1.high
            gap_ticks = gap_size / self.market.tick_size

            if gap_ticks >= self.sc.min_fvg_ticks:
                return FairValueGap(
                    top=c3.low,
                    bottom=c1.high,
                    direction=Direction.LONG,
                    candle_1=c1,
                    candle_2=c2,
                    candle_3=c3,
                    timestamp=c3.timestamp,
                )

        # Bearish FVG: gap between c3.high and c1.low
        if c1.low > c3.high:
            gap_size = c1.low - c3.high
            gap_ticks = gap_size / self.market.tick_size

            if gap_ticks >= self.sc.min_fvg_ticks:
                return FairValueGap(
                    top=c1.low,
                    bottom=c3.high,
                    direction=Direction.SHORT,
                    candle_1=c1,
                    candle_2=c2,
                    candle_3=c3,
                    timestamp=c3.timestamp,
                )

        return None

    def is_fvg_in_range(self, fvg: FairValueGap) -> bool:
        """Check if an FVG is back inside the first candle range"""
        if self.first_candle is None:
            return False

        fc = self.first_candle
        # FVG should overlap with the first candle range
        return fvg.bottom < fc.high and fvg.top > fc.low

    def score_fvg_quality(self, fvg: FairValueGap) -> dict:
        """
        Score the quality of an FVG using 5 playbook checks.
        Returns {'points': int, 'is_trap': bool, 'reasons': List[str]}

        Maximum points: 90 (displacement candle 30 + gap size 20 + anchor 25 + c3 confirm 15)
        Trap FVG (Consequent Encroachment) returns is_trap=True, points=0 — hard fail.
        """
        if self.first_candle is None:
            return {'points': 0, 'is_trap': False, 'reasons': ['No first candle data available']}

        reasons = []
        points  = 0
        fc      = self.first_candle

        # ── Check 1: Consequent Encroachment (HARD FILTER) ────────────────────
        if fvg.direction == Direction.LONG:
            if fvg.candle_1.high >= fvg.candle_3.low:
                return {
                    'points': 0, 'is_trap': True,
                    'reasons': ['TRAP FVG — Consequent Encroachment: c1 wick overlaps c3 wick (institutional indecision)'],
                }
        else:
            if fvg.candle_1.low <= fvg.candle_3.high:
                return {
                    'points': 0, 'is_trap': True,
                    'reasons': ['TRAP FVG — Consequent Encroachment: c1 wick overlaps c3 wick (institutional indecision)'],
                }
        reasons.append('Clean FVG — no consequent encroachment')

        # ── Check 2: Displacement candle body strength (+30 max) ──────────────
        c2 = fvg.candle_2
        if c2.total_range > 0:
            body_ratio = c2.body_size / c2.total_range
            if body_ratio >= 0.70:
                points += 30
                reasons.append(f'Very strong displacement candle ({body_ratio*100:.0f}% body)')
            elif body_ratio >= 0.50:
                points += 20
                reasons.append(f'Moderate displacement candle ({body_ratio*100:.0f}% body)')
            elif body_ratio >= 0.35:
                points += 10
                reasons.append(f'Weak displacement candle ({body_ratio*100:.0f}% body) — borderline')
            else:
                reasons.append(f'Very weak displacement ({body_ratio*100:.0f}% body) — flag low confidence')
        else:
            reasons.append('Doji displacement candle — indecision')

        # ── Check 3: FVG gap size relative to first candle (+20 max) ──────────
        if fc.range_size > 0:
            size_ratio = fvg.size / fc.range_size
            if size_ratio >= 0.15:
                points += 20
                reasons.append(f'Substantial FVG ({size_ratio*100:.0f}% of first candle range)')
            elif size_ratio >= 0.08:
                points += 10
                reasons.append(f'Adequate FVG ({size_ratio*100:.0f}% of first candle range)')
            else:
                reasons.append(f'Small FVG ({size_ratio*100:.1f}% of range) — noise risk')
        else:
            reasons.append('Cannot measure FVG relative size — zero range')

        # ── Check 4: FVG anchored at the range break level (+25 max) ──────────
        anchor_tol = self.market.tick_size * 3
        if fvg.direction == Direction.LONG:
            if abs(fvg.bottom - fc.low) <= anchor_tol:
                points += 25
                reasons.append('FVG anchored at first candle low — structural break confirmed')
            else:
                reasons.append('FVG floating above range level (not anchored) — weaker setup')
        else:
            if abs(fvg.top - fc.high) <= anchor_tol:
                points += 25
                reasons.append('FVG anchored at first candle high — structural break confirmed')
            else:
                reasons.append('FVG floating below range level (not anchored) — weaker setup')

        # ── Check 5: Candle 3 direction confirmation (+15 max) ────────────────
        c3 = fvg.candle_3
        if fvg.direction == Direction.LONG and c3.is_bullish:
            points += 15
            reasons.append('Candle 3 bullish — confirms displacement direction')
        elif fvg.direction == Direction.SHORT and c3.is_bearish:
            points += 15
            reasons.append('Candle 3 bearish — confirms displacement direction')
        else:
            reasons.append('Candle 3 indecision — does not confirm displacement direction')

        return {'points': points, 'is_trap': False, 'reasons': reasons}

    def classify_day_type(self) -> DayType:
        """Classify today's price action based on first candle range breaks."""
        if self.first_candle is None:
            return DayType.UNKNOWN
        if self.range_broken_high and self.range_broken_low:
            return DayType.MIXUP
        if self.range_broken_high or self.range_broken_low:
            return DayType.TRENDING
        return DayType.STEADY

    def calculate_confidence(self, fvg: FairValueGap, current_time: datetime) -> ConfidenceScore:
        """
        Composite confidence score for the current trade setup.
        Combines FVG quality, displacement strength, bias strength,
        kill zone timing, and structural alignment.

        Max possible: 100 points
          FVG quality:         0-90
          Displacement:        0-20
          Bias strength:       0-15
          Kill zone bonus:     0-10
        """
        reasons = []

        # 1. FVG Quality sub-score
        fvg_result = self.score_fvg_quality(fvg)

        if fvg_result['is_trap']:
            return ConfidenceScore(
                grade=ConfidenceGrade.TRAP,
                score=0,
                fvg_quality=0,
                displacement_quality=0.0,
                bias_strength=self._bias_strength,
                in_kill_zone=False,
                bias_aligned=False,
                trap_fvg=True,
                reasons=fvg_result['reasons'],
                recommendation='SKIP — Trap FVG detected (Consequent Encroachment)',
            )

        fvg_points = fvg_result['points']
        reasons.extend(fvg_result['reasons'])

        # 2. Displacement quality (0-20 points)
        disp_points = int(self._displacement_quality * 20)
        if self._displacement_quality >= 0.875:
            reasons.append(f'Strong displacement quality ({self._displacement_quality*100:.0f}%)')
        elif self._displacement_quality >= 0.625:
            reasons.append(f'Moderate displacement quality ({self._displacement_quality*100:.0f}%)')
        else:
            reasons.append(f'Weak displacement ({self._displacement_quality*100:.0f}%) — reduces confidence')

        # 3. Bias strength (0-15 points)
        bias_points = int(self._bias_strength * 15)
        if self._bias_strength >= 0.8:
            reasons.append('Strong HTF bias — high conviction directional move')
        elif self._bias_strength >= 0.5:
            reasons.append('Moderate HTF bias')
        else:
            reasons.append('Weak HTF bias — limited conviction')

        # 4. Kill zone timing bonus (+10)
        current_t = current_time.time()
        in_kill_zone = (
            (self.session.ny_am_kill_zone_start <= current_t <= self.session.ny_am_kill_zone_end)
            or (self.session.ny_pm_kill_zone_start <= current_t <= self.session.ny_pm_kill_zone_end)
            or (self.session.london_kill_zone_start <= current_t <= self.session.london_kill_zone_end)
        )
        kill_points = 10 if in_kill_zone else 0
        reasons.append('Inside kill zone — optimal institutional timing' if in_kill_zone
                       else 'Outside peak kill zone hours')

        # 5. Bias alignment (sweep direction vs HTF bias)
        # Break HIGH + SHORT FVG = swept buy-side, distributing lower (matches bearish bias)
        # Break LOW + LONG FVG = swept sell-side, accumulating higher (matches bullish bias)
        bias_aligned = (
            (self.range_broken_high and self.bias == Direction.SHORT) or
            (self.range_broken_low  and self.bias == Direction.LONG)
        )
        reasons.append('HTF bias aligned with sweep direction' if bias_aligned
                       else 'HTF bias vs sweep direction mismatch — weaker probability')

        # 6. Mixup day cap
        day_type = self.classify_day_type()
        if day_type == DayType.MIXUP:
            reasons.append('Mixup day — both H and L broken, choppy conditions detected')

        # Composite score
        total = min(100, fvg_points + disp_points + bias_points + kill_points)

        # Apply caps for adverse conditions
        if not bias_aligned:
            total = min(total, 74)
        if day_type == DayType.MIXUP:
            total = min(total, 59)
        if self._bias_strength < 0.5:
            total = min(total, 59)

        # Grade
        if total >= 80:
            grade = ConfidenceGrade.A_PLUS
            recommendation = 'HIGH CONFIDENCE — A+ setup, all key filters passed'
        elif total >= 60:
            grade = ConfidenceGrade.B
            recommendation = 'GOOD SETUP — B grade, minor weaknesses present'
        elif total >= 40:
            grade = ConfidenceGrade.C
            recommendation = 'MARGINAL SETUP — C grade, proceed with caution'
        else:
            grade = ConfidenceGrade.D
            recommendation = 'WEAK SETUP — D grade, skip recommended'

        return ConfidenceScore(
            grade=grade,
            score=total,
            fvg_quality=fvg_points,
            displacement_quality=self._displacement_quality,
            bias_strength=self._bias_strength,
            in_kill_zone=in_kill_zone,
            bias_aligned=bias_aligned,
            trap_fvg=False,
            reasons=reasons,
            recommendation=recommendation,
        )

    # ──────────────────────────────────────────────
    # STEP 6: SIGNAL GENERATION
    # ──────────────────────────────────────────────

    def generate_signal(
        self,
        current_time: datetime,
        candles: List[Candle],
    ) -> TradeSignal:
        """
        Main signal generation — the heart of the strategy.

        Runs through the complete checklist and either generates
        a trade signal or returns NO_SIGNAL with reasons.
        """
        no_trade_reasons = []

        # ── PRE-CHECKS ──

        # Check: Already generated a signal today?
        if self.signal_generated:
            return TradeSignal(
                signal_type=SignalType.NO_SIGNAL,
                no_trade_reasons=[NoTradeReason.DAILY_TRADE_LIMIT],
                timestamp=current_time,
            )

        # Check: Daily limits
        if self.daily_stats.wins >= self.sc.max_wins_per_day:
            no_trade_reasons.append(NoTradeReason.DAILY_WIN_LIMIT)
        if self.daily_stats.losses >= self.sc.max_losses_per_day:
            no_trade_reasons.append(NoTradeReason.DAILY_LOSS_LIMIT)
        if self.daily_stats.total_trades >= self.sc.max_trades_per_day:
            no_trade_reasons.append(NoTradeReason.DAILY_TRADE_LIMIT)

        # Check: Monday filter
        if self.sc.skip_mondays and current_time.weekday() == 0:
            no_trade_reasons.append(NoTradeReason.MONDAY)

        # Check: Past stop trading time
        current_t = current_time.time()
        if current_t >= self.sc.stop_trading_time:
            no_trade_reasons.append(NoTradeReason.PAST_STOP_TIME)

        # Check: Kill zone filter
        if self.sc.kill_zone_only:
            in_kill_zone = (
                (self.session.ny_am_kill_zone_start <= current_t <= self.session.ny_am_kill_zone_end)
                or (self.session.ny_pm_kill_zone_start <= current_t <= self.session.ny_pm_kill_zone_end)
                or (self.session.london_kill_zone_start <= current_t <= self.session.london_kill_zone_end)
            )
            if not in_kill_zone:
                no_trade_reasons.append(NoTradeReason.OUTSIDE_KILL_ZONE)

        # Check: Bias
        if self.bias == Direction.NONE:
            no_trade_reasons.append(NoTradeReason.NO_BIAS)

        # If any hard no-trade reason, return immediately
        if no_trade_reasons:
            return TradeSignal(
                signal_type=SignalType.SKIP,
                no_trade_reasons=no_trade_reasons,
                timestamp=current_time,
            )

        # ── CORE LOGIC ──

        # Check: First candle must be marked
        if self.first_candle is None:
            return TradeSignal(
                signal_type=SignalType.NO_SIGNAL,
                timestamp=current_time,
                notes="Waiting for first candle to close",
            )

        # Check: Need displacement break
        if not self.range_broken_high and not self.range_broken_low:
            # Try to detect displacement from latest candle
            if candles:
                is_disp, disp_dir = self.check_displacement_break(candles[-1])
                if not is_disp:
                    return TradeSignal(
                        signal_type=SignalType.NO_SIGNAL,
                        no_trade_reasons=[NoTradeReason.NO_DISPLACEMENT],
                        timestamp=current_time,
                    )

        # Check: Need FVG
        if len(candles) >= 3:
            fvg = self.detect_fvg(candles)

            if fvg is None:
                return TradeSignal(
                    signal_type=SignalType.NO_SIGNAL,
                    no_trade_reasons=[NoTradeReason.NO_FVG],
                    timestamp=current_time,
                )

            # FVG must be back in the range
            if not self.is_fvg_in_range(fvg):
                return TradeSignal(
                    signal_type=SignalType.NO_SIGNAL,
                    no_trade_reasons=[NoTradeReason.NO_FVG],
                    timestamp=current_time,
                    notes="FVG found but not within first candle range",
                )

            # ── DETERMINE TRADE DIRECTION ──
            # Key Casper SMC principle:
            # Break HIGH then retrace = swept buy-side liquidity → SHORT
            # Break LOW then retrace = swept sell-side liquidity → LONG

            if self.range_broken_high and fvg.direction == Direction.SHORT:
                # Swept high, now distributing lower → SHORT
                trade_direction = Direction.SHORT
            elif self.range_broken_low and fvg.direction == Direction.LONG:
                # Swept low, now distributing higher → LONG
                trade_direction = Direction.LONG
            else:
                # FVG direction doesn't match the sweep pattern
                return TradeSignal(
                    signal_type=SignalType.NO_SIGNAL,
                    no_trade_reasons=[NoTradeReason.NO_FVG],
                    timestamp=current_time,
                    notes="FVG direction doesn't align with sweep pattern",
                )

            # ── CALCULATE LEVELS ──
            entry_price = self._calculate_entry(fvg, trade_direction)
            stop_loss = self._calculate_stop_loss(trade_direction)
            take_profit = self._calculate_take_profit(
                entry_price, stop_loss, trade_direction
            )
            risk = abs(entry_price - stop_loss)
            reward = abs(take_profit - entry_price)
            rr = reward / risk if risk > 0 else 0

            # Validate R:R
            if rr < self.sc.min_risk_reward:
                return TradeSignal(
                    signal_type=SignalType.NO_SIGNAL,
                    timestamp=current_time,
                    notes=f"R:R of {rr:.1f} below minimum {self.sc.min_risk_reward}",
                )

            # ── GENERATE SIGNAL ──
            signal_type = (
                SignalType.ENTER_LONG if trade_direction == Direction.LONG
                else SignalType.ENTER_SHORT
            )

            # Calculate confidence score
            confidence = self.calculate_confidence(fvg, current_time)

            # Suppress TRAP FVGs entirely
            if confidence.trap_fvg:
                return TradeSignal(
                    signal_type=SignalType.NO_SIGNAL,
                    no_trade_reasons=[NoTradeReason.NO_FVG],
                    timestamp=current_time,
                    notes='Trap FVG suppressed — Consequent Encroachment detected',
                )

            self.signal_generated = True

            return TradeSignal(
                signal_type=signal_type,
                direction=trade_direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                risk_reward=rr,
                fvg=fvg,
                first_candle=self.first_candle,
                timestamp=current_time,
                notes=f"First Candle Rule — {'Long' if trade_direction == Direction.LONG else 'Short'} via FVG",
                confidence=confidence,
            )

        return TradeSignal(
            signal_type=SignalType.NO_SIGNAL,
            timestamp=current_time,
            notes="Insufficient candles for FVG detection",
        )

    # ──────────────────────────────────────────────
    # CALCULATION HELPERS
    # ──────────────────────────────────────────────

    def _calculate_entry(self, fvg: FairValueGap, direction: Direction) -> float:
        """Calculate entry price based on FVG"""
        if self.sc.fvg_entry_level == "top":
            return fvg.top if direction == Direction.SHORT else fvg.bottom
        elif self.sc.fvg_entry_level == "middle":
            return fvg.midpoint
        else:  # bottom
            return fvg.bottom if direction == Direction.SHORT else fvg.top

    def _calculate_stop_loss(self, direction: Direction) -> float:
        """Calculate stop loss based on first candle"""
        if self.first_candle is None:
            return 0.0

        fc = self.first_candle
        buffer = self.sc.stop_loss_buffer_ticks * self.market.tick_size

        if self.sc.stop_loss_method == "wick":
            if direction == Direction.LONG:
                return fc.low - buffer
            else:
                return fc.high + buffer

        elif self.sc.stop_loss_method == "body":
            if direction == Direction.LONG:
                return fc.candle.body_low - buffer
            else:
                return fc.candle.body_high + buffer

        else:  # structure — fallback to wick
            if direction == Direction.LONG:
                return fc.low - buffer
            else:
                return fc.high + buffer

    def _calculate_take_profit(
        self,
        entry: float,
        stop_loss: float,
        direction: Direction,
    ) -> float:
        """Calculate take profit — prefer liquidity targets, fallback to R:R"""
        risk = abs(entry - stop_loss)

        # Try to find a liquidity target
        if self.sc.use_liquidity_targets and self.liquidity_levels:
            target = self._find_best_liquidity_target(entry, direction)
            if target is not None:
                potential_reward = abs(target - entry)
                if potential_reward / risk >= self.sc.min_risk_reward:
                    return target

        # Fallback: fixed R:R
        reward = risk * self.sc.target_risk_reward

        if direction == Direction.LONG:
            return entry + reward
        else:
            return entry - reward

    def _find_best_liquidity_target(
        self,
        entry: float,
        direction: Direction,
    ) -> Optional[float]:
        """Find the nearest liquidity level in the trade direction"""
        candidates = []

        for level in self.liquidity_levels:
            if direction == Direction.LONG and level.price > entry:
                candidates.append(level)
            elif direction == Direction.SHORT and level.price < entry:
                candidates.append(level)

        if not candidates:
            return None

        # Sort by distance from entry (nearest first)
        candidates.sort(key=lambda l: abs(l.price - entry))
        return candidates[0].price

    # ──────────────────────────────────────────────
    # TRADE MANAGEMENT
    # ──────────────────────────────────────────────

    def check_exit(
        self,
        candle: Candle,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        direction: Direction,
    ) -> Tuple[bool, str, float]:
        """
        Check if current candle triggers an exit.

        Returns: (should_exit, reason, exit_price)
        """
        if direction == Direction.LONG:
            # Stop loss hit
            if candle.low <= stop_loss:
                return True, "STOP_LOSS", stop_loss

            # Take profit hit
            if candle.high >= take_profit:
                return True, "TAKE_PROFIT", take_profit

        elif direction == Direction.SHORT:
            # Stop loss hit
            if candle.high >= stop_loss:
                return True, "STOP_LOSS", stop_loss

            # Take profit hit
            if candle.low <= take_profit:
                return True, "TAKE_PROFIT", take_profit

        # EOD close check
        current_t = candle.timestamp.time()
        if current_t >= self.sc.close_all_time:
            if direction == Direction.LONG:
                return True, "EOD_CLOSE", candle.close
            else:
                return True, "EOD_CLOSE", candle.close

        return False, "", 0.0

    def calculate_position_size(self, entry: float, stop_loss: float) -> int:
        """Calculate number of contracts based on risk %"""
        account_balance = self.config.paper.starting_balance
        # In a live system, this would be current equity

        risk_dollars = account_balance * (self.sc.risk_per_trade_pct / 100)
        risk_per_contract = abs(entry - stop_loss) * self.market.point_value

        if risk_per_contract == 0:
            return 0

        contracts = int(risk_dollars / risk_per_contract)
        return max(1, contracts)  # Minimum 1 contract
