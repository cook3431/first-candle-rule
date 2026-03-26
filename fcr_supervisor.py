"""
fcr_supervisor.py — Health monitor. Runs every 30 seconds via cron.

Single responsibility: make sure the right processes are running. Self-heal
if anything crashes. Cron fires this every minute; the script runs its check
twice (with a 30-second sleep) to achieve 30-second polling frequency.

Responsibilities:
  - If phase=IN_TRADE and exit_monitor not running → restart it
  - If pending files exist and executor not running → trigger executor
  - If phase=SCANNING and past 15:00 ET → force DONE
  - If phase=IN_TRADE and past 15:55 ET → launch emergency EOD close
  - Write heartbeat to state/supervisor-heartbeat.json
"""

import sys, os, json, logging, subprocess, time, signal
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

import pytz

ROOT      = Path(__file__).parent
STATE_DIR = ROOT / "state"
LOGS_DIR  = ROOT / "logs"
ET        = pytz.timezone("America/New_York")

DONE_TIME     = dtime(15, 0)    # No new entries after this
EOD_CLOSE_TIME= dtime(15, 55)   # Force EOD close after this

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SUPERVISOR] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "supervisor.log"),
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


def _is_running(pid_file: Path) -> bool:
    """Check if the process recorded in a PID file is still alive."""
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)   # Signal 0 = just check existence
        return True
    except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
        return False


def _launch(script_name: str, log_file: str):
    """Launch a script as a background subprocess."""
    script = ROOT / script_name
    lf     = LOGS_DIR / log_file
    with open(lf, "a") as f:
        subprocess.Popen(
            [sys.executable, str(script)],
            stdout=f, stderr=f,
            cwd=str(ROOT),
        )
    log.info(f"Launched {script_name}")


def _pending_files_exist() -> bool:
    pending_dir = STATE_DIR / "pending"
    return any(pending_dir.glob("*.json"))


def _trade_active() -> bool:
    at = _read_json(STATE_DIR / "active-trade.json")
    return bool(at and at.get("qty", 0) > 0)


def _write_heartbeat(now_et: datetime):
    _write_json(STATE_DIR / "supervisor-heartbeat.json", {
        "last_seen": now_et.isoformat(),
        "pid":       os.getpid(),
    })


# ── Single check cycle ────────────────────────────────────────────────────────

def check_system():
    now_et = datetime.now(ET)
    state  = _read_json(STATE_DIR / "system-state.json") or {}
    phase  = state.get("phase", "IDLE")

    _write_heartbeat(now_et)
    log.debug(f"Phase={phase}  time={now_et.strftime('%H:%M')}")

    # ── EOD safety net: force close if in trade past 15:55 ────────────────────
    if phase == "IN_TRADE" and now_et.time() >= EOD_CLOSE_TIME:
        log.warning("EOD safety net: IN_TRADE past 15:55 ET — launching exit monitor for EOD close")
        exit_pid = STATE_DIR / "exit_monitor.pid"
        if not _is_running(exit_pid):
            _launch("fcr_exit_monitor.py", "exit_monitor.log")
        return

    # ── Past 15:00 ET: no new entries ─────────────────────────────────────────
    if now_et.time() >= DONE_TIME and phase in ("SCANNING", "SIGNAL_PENDING"):
        log.info(f"Past {DONE_TIME} ET — marking DONE")
        _set_phase("DONE")
        return

    # ── IN_TRADE: ensure exit monitor is alive ────────────────────────────────
    if phase == "IN_TRADE":
        exit_pid = STATE_DIR / "exit_monitor.pid"
        if not _is_running(exit_pid):
            log.warning("Exit monitor not running while IN_TRADE — restarting")
            _launch("fcr_exit_monitor.py", "exit_monitor.log")
        return

    # ── SIGNAL_PENDING: trigger executor if not already running ───────────────
    if phase in ("SCANNING", "SIGNAL_PENDING") and _pending_files_exist():
        exec_pid = STATE_DIR / "executor.pid"
        if not _is_running(exec_pid) and not _trade_active():
            log.info("Pending signals found — launching executor")
            _launch("fcr_executor.py", "executor.log")
        return

    # ── Stale SIGNAL_PENDING with no pending files ────────────────────────────
    if phase == "SIGNAL_PENDING" and not _pending_files_exist():
        log.info("Phase=SIGNAL_PENDING but no pending files — reverting to SCANNING")
        _set_phase("SCANNING")


# ── Main — runs twice per minute via single cron entry ───────────────────────

def main():
    check_system()
    time.sleep(30)
    check_system()


if __name__ == "__main__":
    main()
