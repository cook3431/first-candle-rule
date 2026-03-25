"""
broker.py — Alpaca paper/live execution layer for the First Candle Rule dashboard.

Environment variables (set in Vercel → Settings → Environment Variables):
  ALPACA_API_KEY    — from Alpaca paper-trading dashboard (starts with PK...)
  ALPACA_SECRET_KEY — from Alpaca paper-trading dashboard
  ALPACA_PAPER      — "true" (default) for paper trading, "false" for live
"""

import os
import math

# ── Graceful import: if alpaca-py not yet installed, degrade cleanly ──────────
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        LimitOrderRequest,
        TakeProfitRequest,
        StopLossRequest,
    )
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────

def _is_futures(symbol: str) -> bool:
    """Alpaca does not support futures — detect and block them cleanly."""
    return symbol.upper().endswith("=F") or symbol.upper().endswith("=f")


def _get_client():
    """Return a configured TradingClient, or None if credentials not set."""
    if not ALPACA_AVAILABLE:
        return None
    api_key    = os.environ.get("ALPACA_API_KEY",    "").strip()
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        return None
    paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
    return TradingClient(api_key, secret_key, paper=paper)


def broker_status() -> dict:
    """
    Return broker configuration status.
    Called on every page load — the result controls whether the toggle appears.
    """
    has_key  = bool(os.environ.get("ALPACA_API_KEY",    "").strip())
    has_sec  = bool(os.environ.get("ALPACA_SECRET_KEY", "").strip())
    is_paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
    return {
        "configured":    has_key and has_sec and ALPACA_AVAILABLE,
        "paper":         is_paper,
        "sdk_available": ALPACA_AVAILABLE,
        "keys_set":      has_key and has_sec,
    }


def calculate_qty(entry: float, stop_loss: float, risk_dollars: float = 100.0) -> int:
    """
    Calculate share quantity based on fixed dollar risk.
      qty = floor(risk_dollars / |entry - stop_loss|)
    Minimum 1 share.  Returns 0 only when risk-per-share is zero (invalid signal).
    """
    risk_per_share = abs(entry - stop_loss)
    if risk_per_share <= 0:
        return 0
    return max(1, math.floor(risk_dollars / risk_per_share))


def execute_bracket_order(
    symbol: str,
    direction: str,          # "LONG" or "SHORT"
    entry: float,
    stop_loss: float,
    take_profit: float,
    risk_dollars: float = 100.0,
) -> dict:
    """
    Place a bracket limit order via Alpaca (DAY order).

    The bracket order places three legs in one API call:
      1. Parent limit order at `entry`
      2. Take-profit limit order at `take_profit`
      3. Stop-loss stop order at `stop_loss`

    Returns a JSON-serialisable dict — never raises.
    """
    # ── Futures guard ──
    if _is_futures(symbol):
        return {
            "success":  False,
            "order_id": None,
            "qty":      0,
            "error":    f"Alpaca does not support futures ({symbol}). Switch to a stock/ETF.",
            "paper":    True,
        }

    # ── Config guard ──
    st = broker_status()
    if not st["sdk_available"]:
        return {"success": False, "order_id": None, "qty": 0,
                "error": "alpaca-py not installed. Add alpaca-py>=0.26.0 to requirements.txt.", "paper": True}
    if not st["keys_set"]:
        return {"success": False, "order_id": None, "qty": 0,
                "error": "ALPACA_API_KEY / ALPACA_SECRET_KEY not set in Vercel environment variables.", "paper": True}

    # ── Position sizing ──
    qty = calculate_qty(entry, stop_loss, risk_dollars)
    if qty == 0:
        return {"success": False, "order_id": None, "qty": 0,
                "error": "Invalid signal: entry price equals stop loss.", "paper": st["paper"]}

    client = _get_client()
    if not client:
        return {"success": False, "order_id": None, "qty": 0,
                "error": "Could not create Alpaca client — check API keys.", "paper": st["paper"]}

    side    = OrderSide.BUY  if direction == "LONG" else OrderSide.SELL
    entry_r = round(entry,      2)
    sl_r    = round(stop_loss,  2)
    tp_r    = round(take_profit, 2)

    try:
        order = client.submit_order(
            LimitOrderRequest(
                symbol        = symbol,
                qty           = qty,
                side          = side,
                time_in_force = TimeInForce.DAY,
                limit_price   = entry_r,
                order_class   = OrderClass.BRACKET,
                take_profit   = TakeProfitRequest(limit_price=tp_r),
                stop_loss     = StopLossRequest(stop_price=sl_r),
            )
        )
        return {
            "success":          True,
            "order_id":         str(order.id),
            "client_order_id":  str(order.client_order_id),
            "symbol":           symbol,
            "side":             direction,
            "qty":              qty,
            "entry":            entry_r,
            "stop_loss":        sl_r,
            "take_profit":      tp_r,
            "status":           str(order.status),
            "paper":            st["paper"],
            "error":            None,
        }
    except Exception as exc:
        # Surface Alpaca API errors cleanly (market closed, insufficient
        # buying power, symbol not tradeable, etc.)
        return {
            "success":  False,
            "order_id": None,
            "qty":      qty,
            "error":    str(exc),
            "paper":    st["paper"],
        }


def get_positions() -> dict:
    """Return all currently open positions."""
    st = broker_status()
    if not st["configured"]:
        return {"success": False, "positions": [], "error": "Broker not configured.", "paper": True}

    client = _get_client()
    if not client:
        return {"success": False, "positions": [], "error": "Could not create Alpaca client.", "paper": True}

    try:
        raw = client.get_all_positions()
        positions = []
        for p in raw:
            positions.append({
                "symbol":          p.symbol,
                "qty":             float(p.qty),
                "side":            str(p.side.value if hasattr(p.side, 'value') else p.side),
                "avg_entry":       float(p.avg_entry_price) if p.avg_entry_price else 0.0,
                "current_price":   float(p.current_price)   if p.current_price   else None,
                "market_value":    float(p.market_value)    if p.market_value    else None,
                "unrealized_pl":   float(p.unrealized_pl)   if p.unrealized_pl   else 0.0,
                "unrealized_plpc": float(p.unrealized_plpc) if p.unrealized_plpc else 0.0,
            })
        return {"success": True, "positions": positions, "paper": st["paper"]}
    except Exception as exc:
        return {"success": False, "positions": [], "error": str(exc), "paper": st["paper"]}


def get_account() -> dict:
    """Return account balance and buying power."""
    st = broker_status()
    if not st["configured"]:
        return {"success": False, "error": "Broker not configured.", "paper": True}

    client = _get_client()
    if not client:
        return {"success": False, "error": "Could not create Alpaca client.", "paper": True}

    try:
        a = client.get_account()
        return {
            "success":         True,
            "cash":            float(a.cash),
            "buying_power":    float(a.buying_power),
            "equity":          float(a.equity),
            "portfolio_value": float(a.portfolio_value),
            "paper":           st["paper"],
            "status":          str(a.status.value if hasattr(a.status, 'value') else a.status),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "paper": st["paper"]}


def cancel_order(order_id: str) -> dict:
    """Cancel a pending order by ID."""
    st = broker_status()
    if not st["configured"]:
        return {"success": False, "error": "Broker not configured."}

    client = _get_client()
    if not client:
        return {"success": False, "error": "Could not create Alpaca client."}

    try:
        client.cancel_order_by_id(order_id)
        return {"success": True, "order_id": order_id}
    except Exception as exc:
        return {"success": False, "error": str(exc), "order_id": order_id}
