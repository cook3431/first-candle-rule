"""
fcr_executor.py — Trade executor. Triggered by fcr_supervisor.py.

Reads the first valid pending signal, validates it, places the order
via Alpaca, writes state/active-trade.json, and launches fcr_exit_monitor.py.

Safety rules enforced here:
  - No trades after 15:00 ET
  - Max 1 trade at a time (active-trade.json check)
  - Max 1 win per day
  - Max 2 losses per day
  - Don't chase (price >0.5% past entry)
  - Not expired
"""

import sys, os, json, logging, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

import pytz

from broker import execute_entry_with_trail, execute_bracket_order
from data_client import fetch_current_price

# ── Paths / config ────────────────────────────────────────────────────────────
ROOT      = Path(__file__).parent
STATE_DIR = ROOT / "state"
LOGS_DIR  = ROOT / "logs"
TRADES_DIR= ROOT / "trades"
ET        = pytz.timezone("America/New_York")

STOP_TRADE_TIME = dtime(15, 0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EXECUTOR] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "executor.log"),
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


def _trade_active() -> bool:
    at = _read_json(STATE_DIR / "active-trade.json")
    return bool(at and at.get("qty", 0) > 0)


def _today_stats() -> dict:
    """Count wins/losses from trades/index.json for today."""
    today = str(datetime.now(ET).date())
    try:
        idx = _read_json(TRADES_DIR / "index.json") or {}
        trades = [t for t in idx.get("trades", []) if t.get("date") == today]
        wins   = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in trades if t.get("pnl", 0) <= 0)
        return {"wins": wins, "losses": losses, "total": len(trades)}
    except Exception:
        return {"wins": 0, "losses": 0, "total": 0}


def _already_traded(symbol: str) -> bool:
    f = STATE_DIR / "traded-today.txt"
    return f.exists() and symbol in f.read_text().splitlines()


def _launch_exit_monitor():
    """Launch fcr_exit_monitor.py as a background subprocess."""
    script = ROOT / "fcr_exit_monitor.py"
    log_file = LOGS_DIR / "exit_monitor.log"
    with open(log_file, "a") as lf:
        subprocess.Popen(
            [sys.executable, str(script)],
            stdout=lf, stderr=lf,
            cwd=str(ROOT),
        )
    log.info("fcr_exit_monitor.py launched")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now_et = datetime.now(ET)
    log.info(f"Executor running  {now_et.strftime('%H:%M %Z')}")

    # Write PID
    (STATE_DIR / "executor.pid").write_text(str(os.getpid()))

    # ── Time gate ─────────────────────────────────────────────────────────────
    if now_et.time() >= STOP_TRADE_TIME:
        log.info(f"Past {STOP_TRADE_TIME} ET — no new trades")
        return

    # ── Active trade gate ─────────────────────────────────────────────────────
    if _trade_active():
        log.info("Trade already active — not executing another")
        return

    # ── Daily limits ──────────────────────────────────────────────────────────
    stats = _today_stats()
    if stats["wins"] >= 1:
        log.info(f"Daily win limit reached ({stats['wins']} win today) — done for today")
        _set_phase("DONE")
        return
    if stats["losses"] >= 2:
        log.info(f"Daily loss limit reached ({stats['losses']} losses today) — done for today")
        _set_phase("DONE")
        return

    # ── Find pending signals ──────────────────────────────────────────────────
    pending_dir  = STATE_DIR / "pending"
    pending_files = sorted(pending_dir.glob("*.json"))

    if not pending_files:
        log.info("No pending signals")
        return

    executed = False
    for pending_path in pending_files:
        signal = _read_json(pending_path)
        if signal is None:
            continue

        symbol = signal.get("symbol", "")
        log.info(f"Processing pending signal: {symbol}")

        # ── Expiry check ──────────────────────────────────────────────────────
        try:
            expires_at = datetime.fromisoformat(signal["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = ET.localize(expires_at)
            if now_et > expires_at:
                log.info(f"  {symbol}: signal expired at {signal['expires_at']} — discarding")
                pending_path.unlink(missing_ok=True)
                continue
        except Exception:
            pass

        # ── Already traded today ──────────────────────────────────────────────
        if _already_traded(symbol):
            log.info(f"  {symbol}: already traded today — discarding pending file")
            pending_path.unlink(missing_ok=True)
            continue

        # ── Don't chase check ─────────────────────────────────────────────────
        try:
            current_price = fetch_current_price(symbol)
        except Exception as e:
            log.warning(f"  {symbol}: could not fetch price ({e}) — skipping")
            continue

        if current_price is None:
            log.warning(f"  {symbol}: no current price available — skipping")
            continue

        entry     = signal["entry_price"]
        direction = signal["direction"]
        overshoot = 0.0
        if direction == "LONG":
            overshoot = (current_price - entry) / entry if entry > 0 else 0
        else:
            overshoot = (entry - current_price) / entry if entry > 0 else 0

        if overshoot > 0.005:
            log.info(
                f"  {symbol}: price {current_price:.2f} has moved "
                f"{overshoot*100:.2f}% past entry {entry:.2f} — don't chase, discarding"
            )
            pending_path.unlink(missing_ok=True)
            continue

        log.info(
            f"  {symbol}: {direction} entry={entry:.2f} sl={signal['stop_loss']:.2f} "
            f"grade={signal['confidence_grade']} score={signal['confidence_score']} "
            f"current={current_price:.2f}"
        )

        # ── Execute ───────────────────────────────────────────────────────────
        risk = float(signal.get("risk_per_trade", 500))
        trail_mode = signal.get("trail_mode", True)

        if trail_mode:
            result = execute_entry_with_trail(
                symbol=symbol,
                direction=direction,
                entry=entry,
                initial_stop=signal["stop_loss"],
                risk_dollars=risk,
            )
        else:
            result = execute_bracket_order(
                symbol=symbol,
                direction=direction,
                entry=entry,
                stop_loss=signal["stop_loss"],
                take_profit=signal["take_profit"],
                risk_dollars=risk,
            )

        if not result.get("success"):
            log.error(f"  {symbol}: broker error — {result.get('error')}")
            # Don't discard pending file — supervisor may retry
            continue

        # ── Write active trade ────────────────────────────────────────────────
        active_trade = {
            "symbol":              symbol,
            "direction":           direction,
            "entry_price":         entry,
            "stop_loss":           signal["stop_loss"],
            "take_profit":         signal["take_profit"],
            "trail_mode":          trail_mode,
            "trail_amount":        result.get("trail_amount"),
            "trail_activated":     False,
            "qty":                 result["qty"],
            "risk_dollars":        risk,
            "alpaca_order_id":     result.get("order_id"),
            "alpaca_stop_order_id":result.get("order_id"),  # updated after fill/trail
            "status":              "PENDING_FILL",
            "entry_time":          datetime.now(ET).isoformat(),
            "confidence_grade":    signal.get("confidence_grade"),
            "confidence_score":    signal.get("confidence_score"),
            "first_candle_high":   signal.get("first_candle_high"),
            "first_candle_low":    signal.get("first_candle_low"),
            "paper":               result.get("paper", True),
        }
        _write_json(STATE_DIR / "active-trade.json", active_trade)

        # ── Consume pending file ──────────────────────────────────────────────
        pending_path.unlink(missing_ok=True)

        _set_phase("IN_TRADE", active_symbol=symbol)

        mode_tag = "[PAPER]" if result.get("paper") else "[LIVE]"
        log.info(
            f"  {mode_tag} ORDER PLACED: {direction} {result['qty']} × {symbol} "
            f"@ {entry:.2f} | SL {signal['stop_loss']:.2f} "
            f"{'| Trail mode' if trail_mode else f'| TP {signal[\"take_profit\"]:.2f}'} "
            f"| ID {(result.get('order_id') or '')[:8]}…"
        )

        # ── Launch exit monitor ───────────────────────────────────────────────
        _launch_exit_monitor()
        executed = True
        break   # One trade per execution run

    if not executed:
        log.info("No signals executed this run")


if __name__ == "__main__":
    main()
