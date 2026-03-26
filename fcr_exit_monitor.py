"""
fcr_exit_monitor.py — Active trade monitor. Launched by fcr_executor.py.

Runs a 30-second loop while a trade is open. Handles all exit conditions:
  - Stop loss hit (price crosses stop)
  - Take profit hit (price crosses TP, non-trail mode)
  - Trail activation at 1R (trail mode only)
  - Trail order filled (trail mode only)
  - EOD force close at 15:55 ET

On any exit:
  - Cancels all open Alpaca orders for the symbol
  - Writes result to trades/index.json
  - Appends row to trades/equity_curve.csv
  - Adds symbol to state/traded-today.txt
  - Resets state/active-trade.json (qty: 0)
  - Sets system-state.json → phase: DONE
"""

import sys, os, json, logging, time, csv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

import pytz

from broker import (
    activate_native_trail,
    cancel_order,
    get_order_status,
    _get_client,
)
from data_client import fetch_current_price

# ── Paths / config ────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent
STATE_DIR  = ROOT / "state"
LOGS_DIR   = ROOT / "logs"
TRADES_DIR = ROOT / "trades"
ET         = pytz.timezone("America/New_York")

EOD_CLOSE_TIME = dtime(15, 55)   # Force-close all positions at this time
POLL_INTERVAL  = 30               # seconds between price checks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EXIT_MONITOR] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "exit_monitor.log"),
    ],
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _set_phase(phase: str, **kwargs):
    existing = _read_json(STATE_DIR / "system-state.json") or {}
    existing.update({"phase": phase, "updated_at": datetime.now(ET).isoformat(), **kwargs})
    _write_json(STATE_DIR / "system-state.json", existing)


def _mark_traded(symbol: str):
    f = STATE_DIR / "traded-today.txt"
    existing = f.read_text().splitlines() if f.exists() else []
    if symbol not in existing:
        existing.append(symbol)
        f.write_text("\n".join(existing) + "\n")


def _reset_active_trade():
    _write_json(STATE_DIR / "active-trade.json", {"qty": 0, "status": "CLOSED"})


def _cancel_all_open_orders(symbol: str):
    """Cancel all open Alpaca orders for a symbol."""
    client = _get_client()
    if not client:
        return
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import OrderStatus
        open_orders = client.get_orders(
            GetOrdersRequest(status=OrderStatus.OPEN, symbols=[symbol])
        )
        for o in open_orders:
            try:
                client.cancel_order_by_id(str(o.id))
                log.info(f"  Cancelled order {str(o.id)[:8]}…")
            except Exception as e:
                log.warning(f"  Could not cancel {str(o.id)[:8]}: {e}")
    except Exception as e:
        log.warning(f"  Could not list open orders for {symbol}: {e}")


def _market_close_position(symbol: str, direction: str, qty: int) -> dict:
    """Place a market order to close the position."""
    client = _get_client()
    if not client:
        return {"success": False, "error": "No client"}
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        close_side = OrderSide.SELL if direction == "LONG" else OrderSide.BUY
        order = client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=close_side,
                time_in_force=TimeInForce.DAY,
            )
        )
        return {"success": True, "order_id": str(order.id)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _get_filled_price_from_alpaca(symbol: str, direction: str) -> Optional[float]:
    """Try to get the actual fill price from Alpaca order history."""
    client = _get_client()
    if not client:
        return None
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders = client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[symbol], limit=5)
        )
        close_side = "sell" if direction == "LONG" else "buy"
        for o in reversed(orders):
            o_side = str(o.side.value if hasattr(o.side, "value") else o.side).lower()
            if o_side == close_side and o.filled_avg_price:
                return float(o.filled_avg_price)
    except Exception:
        pass
    return None


def _write_trade_result(trade: dict, exit_price: float, exit_reason: str):
    """Append a completed trade to trades/index.json and equity_curve.csv."""
    now_et     = datetime.now(ET)
    symbol     = trade["symbol"]
    direction  = trade["direction"]
    entry      = float(trade["entry_price"])
    stop       = float(trade["stop_loss"])
    qty        = int(trade["qty"])
    risk       = float(trade.get("risk_dollars", 500))
    entry_time = trade.get("entry_time", "")

    # P&L
    if direction == "LONG":
        pnl = (exit_price - entry) * qty
    else:
        pnl = (entry - exit_price) * qty

    # R-multiple
    risk_per_share = abs(entry - stop)
    r_multiple = round(pnl / (risk_per_share * qty), 2) if risk_per_share > 0 else 0.0

    # Duration
    try:
        entry_dt = datetime.fromisoformat(entry_time)
        if entry_dt.tzinfo is None:
            entry_dt = ET.localize(entry_dt)
        duration_min = int((now_et - entry_dt).total_seconds() / 60)
    except Exception:
        duration_min = 0

    trade_id = f"{now_et.date()}-{symbol}-{now_et.strftime('%H%M%S')}"

    record = {
        "id":               trade_id,
        "symbol":           symbol,
        "date":             str(now_et.date()),
        "direction":        direction,
        "entry_price":      round(entry, 4),
        "exit_price":       round(exit_price, 4),
        "stop_loss":        round(stop, 4),
        "take_profit":      round(float(trade.get("take_profit", 0)), 4),
        "qty":              qty,
        "pnl":              round(pnl, 2),
        "r_multiple":       r_multiple,
        "exit_reason":      exit_reason,
        "trail_activated":  bool(trade.get("trail_activated", False)),
        "confidence_grade": trade.get("confidence_grade"),
        "confidence_score": trade.get("confidence_score"),
        "entry_time":       entry_time,
        "exit_time":        now_et.isoformat(),
        "duration_minutes": duration_min,
        "paper":            bool(trade.get("paper", True)),
    }

    # ── trades/index.json ─────────────────────────────────────────────────────
    idx_path = TRADES_DIR / "index.json"
    idx = _read_json(idx_path) or {"trades": []}
    idx["trades"].append(record)
    _write_json(idx_path, idx)

    # ── trades/equity_curve.csv ───────────────────────────────────────────────
    csv_path = TRADES_DIR / "equity_curve.csv"
    total_pnl = sum(t.get("pnl", 0) for t in idx["trades"])
    row = [str(now_et.date()), symbol, round(pnl, 2), round(total_pnl, 2)]
    file_exists = csv_path.exists() and csv_path.stat().st_size > 0
    with open(csv_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["date", "symbol", "pnl", "running_total"])
        writer.writerow(row)

    mode_tag = "[PAPER]" if record["paper"] else "[LIVE]"
    log.info(
        f"  {mode_tag} TRADE CLOSED: {symbol} {direction} "
        f"entry={entry:.2f} exit={exit_price:.2f} "
        f"P&L={pnl:+.2f} ({r_multiple:+.2f}R) reason={exit_reason}"
    )


# ── Exit handler ──────────────────────────────────────────────────────────────

def _handle_exit(trade: dict, current_price: float, exit_reason: str):
    """Cancel orders, record trade, reset state."""
    symbol    = trade["symbol"]
    direction = trade["direction"]
    qty       = int(trade["qty"])

    log.info(f"EXIT TRIGGERED: {symbol} — {exit_reason} @ {current_price:.2f}")

    # 1. Cancel all open orders
    _cancel_all_open_orders(symbol)

    # 2. Close position if still open (for stop/EOD cases where Alpaca may not have filled yet)
    if exit_reason in ("STOP_HIT", "EOD_CLOSE", "MANUAL"):
        close_result = _market_close_position(symbol, direction, qty)
        if not close_result["success"]:
            log.warning(f"  Market close order failed: {close_result.get('error')}")
            # Try to get fill price from recent closed orders
            filled = _get_filled_price_from_alpaca(symbol, direction)
            if filled:
                current_price = filled

    # 3. Record trade result
    _write_trade_result(trade, current_price, exit_reason)

    # 4. Mark symbol as traded today
    _mark_traded(symbol)

    # 5. Reset active trade
    _reset_active_trade()

    # 6. Update system state
    _set_phase("DONE", active_symbol=None)

    log.info(f"  State reset. Phase → DONE. {symbol} added to traded-today.txt")


# ── Main monitoring loop ──────────────────────────────────────────────────────

def main():
    now_et = datetime.now(ET)
    log.info(f"Exit monitor started  {now_et.strftime('%H:%M %Z')}")

    # Write PID
    (STATE_DIR / "exit_monitor.pid").write_text(str(os.getpid()))

    consecutive_errors = 0
    MAX_ERRORS = 10

    while True:
        try:
            now_et = datetime.now(ET)

            # ── Read active trade ──────────────────────────────────────────────
            trade = _read_json(STATE_DIR / "active-trade.json")

            if not trade or trade.get("qty", 0) == 0:
                log.info("No active trade — exit monitor shutting down")
                break

            symbol    = trade["symbol"]
            direction = trade["direction"]
            entry     = float(trade["entry_price"])
            stop_loss = float(trade["stop_loss"])
            take_profit = float(trade.get("take_profit", 0) or 0)
            trail_mode  = bool(trade.get("trail_mode", False))
            qty         = int(trade["qty"])
            risk_per_share = abs(entry - stop_loss)

            # ── EOD check ─────────────────────────────────────────────────────
            if now_et.time() >= EOD_CLOSE_TIME:
                log.info(f"EOD force close at {now_et.strftime('%H:%M %Z')}")
                current_price = fetch_current_price(symbol) or entry
                _handle_exit(trade, current_price, "EOD_CLOSE")
                break

            # ── Fetch current price ───────────────────────────────────────────
            current_price = fetch_current_price(symbol)
            if current_price is None:
                log.warning(f"  {symbol}: could not fetch price — skipping this poll")
                consecutive_errors += 1
                if consecutive_errors >= MAX_ERRORS:
                    log.error(f"  {consecutive_errors} consecutive price fetch errors — giving up")
                    break
                time.sleep(POLL_INTERVAL)
                continue

            consecutive_errors = 0

            log.info(
                f"  {symbol} {direction}  price={current_price:.2f}  "
                f"entry={entry:.2f}  stop={stop_loss:.2f}  "
                f"{'trail_mode' if trail_mode else f'tp={take_profit:.2f}'}"
            )

            # ── Stop loss check ───────────────────────────────────────────────
            stop_hit = (
                (direction == "LONG"  and current_price <= stop_loss) or
                (direction == "SHORT" and current_price >= stop_loss)
            )
            if stop_hit:
                _handle_exit(trade, current_price, "STOP_HIT")
                break

            # ── Take profit check (non-trail mode) ───────────────────────────
            if not trail_mode and take_profit > 0:
                tp_hit = (
                    (direction == "LONG"  and current_price >= take_profit) or
                    (direction == "SHORT" and current_price <= take_profit)
                )
                if tp_hit:
                    _handle_exit(trade, current_price, "TP_HIT")
                    break

            # ── Trail mode: activation + fill check ───────────────────────────
            if trail_mode:
                trail_activated = bool(trade.get("trail_activated", False))
                trail_amount    = float(trade.get("trail_amount") or risk_per_share)

                # Check if 1R reached → activate native trail
                if not trail_activated:
                    current_r = 0.0
                    if risk_per_share > 0:
                        if direction == "LONG":
                            current_r = (current_price - entry) / risk_per_share
                        else:
                            current_r = (entry - current_price) / risk_per_share

                    if current_r >= 1.0:
                        log.info(f"  {symbol}: 1R reached ({current_r:.2f}R) — activating native trail")
                        stop_oid = trade.get("alpaca_stop_order_id", "")
                        result = activate_native_trail(
                            symbol=symbol,
                            direction=direction,
                            qty=qty,
                            trail_amount=trail_amount,
                            current_stop_order_id=stop_oid,
                        )
                        if result.get("success"):
                            trade["trail_activated"]     = True
                            trade["alpaca_stop_order_id"] = result["trail_order_id"]
                            _write_json(STATE_DIR / "active-trade.json", trade)
                            log.info(f"  Trail activated — order ID {result['trail_order_id'][:8]}…")
                        else:
                            log.warning(f"  Trail activation failed: {result.get('error')}")

                # Check if trail order was filled
                if trade.get("trail_activated") and trade.get("alpaca_stop_order_id"):
                    status = get_order_status(trade["alpaca_stop_order_id"])
                    if status.get("success") and status.get("status") == "filled":
                        exit_price = status.get("filled_price") or current_price
                        _handle_exit(trade, exit_price, "TRAIL_HIT")
                        break

        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)
            consecutive_errors += 1
            if consecutive_errors >= MAX_ERRORS:
                log.error("Too many errors — exit monitor aborting")
                break

        time.sleep(POLL_INTERVAL)

    log.info("Exit monitor finished")
    # Clean up PID file
    pid_file = STATE_DIR / "exit_monitor.pid"
    if pid_file.exists():
        pid_file.unlink()


if __name__ == "__main__":
    main()
