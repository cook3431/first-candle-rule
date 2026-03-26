"""
trail_manager.py — Trailing stop lifecycle manager.

Singleton called by the Flask app on each price poll.
Handles: activation check and exit detection for FIXED_DOLLAR trailing stops.
Once activated, Alpaca manages stop movement natively.
"""

from typing import Dict, Optional
from datetime import datetime
from models import ActiveTrail, Direction
from broker import activate_native_trail, get_order_status


class TrailManager:

    def __init__(self):
        self._trails: Dict[str, ActiveTrail] = {}  # trade_id → ActiveTrail

    def register(self, trail: ActiveTrail):
        self._trails[trail.trade_id] = trail

    def deregister(self, trade_id: str):
        self._trails.pop(trade_id, None)

    def get(self, trade_id: str) -> Optional[ActiveTrail]:
        return self._trails.get(trade_id)

    def on_price_update(self, trade_id: str, current_price: float, qty: int) -> dict:
        """
        Called on each price update. Returns action taken:
        {"action": "none"|"activated"|"exited", "exit_price": float|None}
        """
        trail = self._trails.get(trade_id)
        if trail is None:
            return {"action": "none"}

        # Update high water mark
        if trail.direction == Direction.LONG:
            trail.high_water_mark = max(trail.high_water_mark, current_price)
        else:
            trail.high_water_mark = min(trail.high_water_mark, current_price)

        current_r = self._calc_r(trail, current_price)

        # Phase 1: Activate trail at 1R
        if not trail.activated and current_r >= 1.0:
            result = activate_native_trail(
                symbol=trail.symbol,
                direction=trail.direction.value,
                qty=qty,
                trail_amount=trail.trail_amount,
                current_stop_order_id=trail.alpaca_stop_order_id,
            )
            if result["success"]:
                trail.activated = True
                trail.alpaca_stop_order_id = result["trail_order_id"]
                trail.last_updated = datetime.now()
                return {"action": "activated", "new_stop_order_id": result["trail_order_id"]}

        # Phase 2: Check if Alpaca trail has fired (exit)
        if trail.activated and trail.alpaca_stop_order_id:
            status = get_order_status(trail.alpaca_stop_order_id)
            if status.get("success") and status.get("status") == "filled":
                exit_price = status.get("filled_price")
                self.deregister(trade_id)
                return {"action": "exited", "exit_price": exit_price}

        return {"action": "none"}

    def _calc_r(self, trail: ActiveTrail, current_price: float) -> float:
        if trail.initial_risk == 0:
            return 0.0
        if trail.direction == Direction.LONG:
            return (current_price - trail.entry_price) / trail.initial_risk
        else:
            return (trail.entry_price - current_price) / trail.initial_risk


# Module-level singleton
trail_manager = TrailManager()
