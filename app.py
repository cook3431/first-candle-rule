"""
First Candle Rule — Vercel Serverless App
"""

import os, sys, io, contextlib
from datetime import datetime
from flask import Flask, render_template, jsonify, request, make_response

# Resolve paths so Vercel can find templates + local modules
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))


# ─── Helpers ────────────────────────────────────────────────────────────────

def run_scan(symbol: str, account_size: int) -> dict:
    """Run the strategy scan and return a JSON-serialisable result dict."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            from live_scanner import scan
            result = scan(symbol, verbose=False, account_size=account_size)
    except Exception as e:
        result = {"status": "ERROR", "reason": str(e)}

    # Attach current price
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        price = getattr(ticker.fast_info, "last_price", None)
        if not price:
            hist = ticker.history(period="1d", interval="1m")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
        if price:
            result["current_price"] = float(price)
    except Exception:
        pass

    try:
        import pytz as _pytz
        _london = _pytz.timezone("Europe/London")
        result["scan_time"] = datetime.now(_london).strftime("%H:%M:%S %Z")
    except Exception:
        result["scan_time"] = datetime.utcnow().strftime("%H:%M:%S UTC")
    result["symbol"] = symbol
    return result


# ─── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/scan")
def api_scan():
    symbol  = request.args.get("symbol", "QQQ").upper().strip()
    account = int(request.args.get("account", "100000"))

    result = run_scan(symbol, account)

    resp = make_response(jsonify(result))
    # Cache at CDN for 55 s so rapid page refreshes don't re-hit yfinance
    # (the browser will still get a fresh result if the user clicks "Refresh")
    resp.headers["Cache-Control"] = "public, s-maxage=55, stale-while-revalidate=60"
    return resp


@app.route("/api/backtest")
def api_backtest():
    symbol   = request.args.get("symbol",   "QQQ").upper().strip()
    date_str = request.args.get("date",     "").strip()
    account  = int(request.args.get("account",  "100000"))
    interval = request.args.get("interval", "5m").strip()

    if interval not in ("1m", "5m", "15m"):
        interval = "5m"

    if not date_str:
        return jsonify({"status": "ERROR", "reason": "date parameter required"})

    try:
        from live_scanner import run_backtest_day
        result = run_backtest_day(symbol, date_str, account_size=float(account),
                                  display_interval=interval)
    except Exception as e:
        result = {"status": "ERROR", "reason": str(e)}

    resp = make_response(jsonify(result))
    resp.headers["Cache-Control"] = "public, s-maxage=3600, stale-while-revalidate=7200"
    return resp


@app.route("/api/price")
def api_price():
    symbol = request.args.get("symbol", "QQQ").upper()
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        price  = getattr(ticker.fast_info, "last_price", None)
        if not price:
            hist = ticker.history(period="1d", interval="1m")
            price = float(hist["Close"].iloc[-1]) if not hist.empty else None
        resp = make_response(jsonify({"price": float(price) if price else None, "symbol": symbol}))
        resp.headers["Cache-Control"] = "public, s-maxage=25"
        return resp
    except Exception as e:
        return jsonify({"price": None, "error": str(e)})


# ─── Broker Routes ───────────────────────────────────────────────────────────

@app.route("/api/broker/status")
def api_broker_status():
    """Check whether Alpaca integration is configured — called on every page load."""
    from broker import broker_status
    return jsonify(broker_status())


@app.route("/api/broker/execute", methods=["POST"])
def api_broker_execute():
    """
    Place a bracket order on Alpaca.
    Body JSON: { symbol, direction, entry, stop_loss, take_profit, risk_dollars }
    risk_dollars: dollars to risk per trade (default 1000, clamped 10–50000).
    """
    from broker import execute_bracket_order
    data = request.get_json(silent=True) or {}

    symbol       = str(data.get("symbol",      "")).upper().strip()
    direction    = str(data.get("direction",   "")).upper().strip()
    entry        = float(data.get("entry",       0) or 0)
    stop_loss    = float(data.get("stop_loss",   0) or 0)
    take_profit  = float(data.get("take_profit", 0) or 0)
    risk_dollars = float(data.get("risk_dollars", 1000) or 1000)
    risk_dollars = max(10.0, min(50000.0, risk_dollars))   # server-side clamp

    if not symbol or direction not in ("LONG", "SHORT") or not entry or not stop_loss or not take_profit:
        return jsonify({"success": False, "error": "Missing or invalid parameters."}), 400

    result = execute_bracket_order(symbol, direction, entry, stop_loss, take_profit, risk_dollars=risk_dollars)
    return jsonify(result), (200 if result["success"] else 422)


@app.route("/api/broker/positions")
def api_broker_positions():
    """Return all currently open positions."""
    from broker import get_positions
    return jsonify(get_positions())


@app.route("/api/broker/account")
def api_broker_account():
    """Return account balance and buying power."""
    from broker import get_account
    return jsonify(get_account())


@app.route("/api/broker/cancel/<order_id>", methods=["DELETE"])
def api_broker_cancel(order_id):
    """Cancel a pending order by ID."""
    from broker import cancel_order
    return jsonify(cancel_order(order_id))


@app.route("/api/broker/execute-trail", methods=["POST"])
def api_broker_execute_trail():
    """OTO entry + initial hard stop for a trailing stop trade. No take-profit."""
    from broker import execute_entry_with_trail
    from trail_manager import trail_manager
    from models import ActiveTrail, Direction

    data         = request.get_json(silent=True) or {}
    symbol       = str(data.get("symbol",      "")).upper().strip()
    direction    = str(data.get("direction",   "")).upper().strip()
    entry        = float(data.get("entry",       0) or 0)
    stop_loss    = float(data.get("stop_loss",   0) or 0)
    risk_dollars = float(data.get("risk_dollars", 100) or 100)
    risk_dollars = max(10.0, min(50000.0, risk_dollars))

    if not symbol or direction not in ("LONG", "SHORT") or not entry or not stop_loss:
        return jsonify({"success": False, "error": "Missing or invalid parameters."}), 400

    result = execute_entry_with_trail(symbol, direction, entry, stop_loss, risk_dollars)

    if result["success"]:
        trail_amount = abs(entry - stop_loss)
        trail = ActiveTrail(
            trade_id             = result["order_id"],
            symbol               = symbol,
            direction            = Direction.LONG if direction == "LONG" else Direction.SHORT,
            entry_price          = entry,
            initial_stop         = stop_loss,
            initial_risk         = trail_amount,
            trail_amount         = trail_amount,
            current_stop         = stop_loss,
            high_water_mark      = entry,
            alpaca_stop_order_id = result["order_id"],
        )
        trail_manager.register(trail)

    return jsonify(result), (200 if result["success"] else 422)


@app.route("/api/broker/trail-update/<trade_id>")
def api_trail_update(trade_id):
    """
    Stateless trail poll — all ActiveTrail state is passed via query params so
    this works correctly on every Vercel cold start.

    Required params: price, sym, dir (LONG|SHORT), entry, stop, trail
    Optional params: qty, risk, act (0|1), hwm, stop_oid
    Returns updated state for the client to persist between polls.
    """
    from trail_manager import trail_manager
    from models import ActiveTrail, Direction

    price = float(request.args.get("price", 0) or 0)
    if not price:
        return jsonify({"success": False, "error": "price required"}), 400

    qty      = int(float(request.args.get("qty",   1) or 1))
    sym      = request.args.get("sym",      "").upper().strip()
    direction= request.args.get("dir",      "LONG").upper()
    entry    = float(request.args.get("entry",  0) or 0)
    stop     = float(request.args.get("stop",   0) or 0)
    trail_amt= float(request.args.get("trail",  0) or 0)
    risk     = float(request.args.get("risk",   0) or 0) or abs(entry - stop)
    trail_amt= trail_amt or risk
    activated= request.args.get("act", "0") == "1"
    hwm      = float(request.args.get("hwm",  entry) or entry)
    stop_oid = request.args.get("stop_oid", "").strip()

    if not sym or not entry or not stop:
        return jsonify({"success": False, "error": "sym, entry, stop required"}), 400

    trail = ActiveTrail(
        trade_id             = trade_id,
        symbol               = sym,
        direction            = Direction.LONG if direction == "LONG" else Direction.SHORT,
        entry_price          = entry,
        initial_stop         = stop,
        initial_risk         = risk,
        trail_amount         = trail_amt,
        current_stop         = stop,
        high_water_mark      = hwm,
        activated            = activated,
        alpaca_stop_order_id = stop_oid,
    )

    result, updated = trail_manager.on_price_update_stateless(trail, price, qty)
    return jsonify({
        "success": True,
        **result,
        # Return updated state so client can persist it and pass back next poll
        "state": {
            "activated":            updated.activated,
            "hwm":                  updated.high_water_mark,
            "alpaca_stop_order_id": updated.alpaca_stop_order_id,
        },
    })


@app.route("/api/portfolio-backtest")
def api_portfolio_backtest():
    """
    Run portfolio backtest comparing Mode A (fixed TP/SL) vs Mode B (trail stop).
    Query params: stocks (comma-sep), days (default 10, max 20), risk (default 100)
    """
    from portfolio_backtest import run_portfolio_backtest, DEFAULT_STOCKS

    stocks_param = request.args.get("stocks", "").strip()
    days_param   = max(1, min(20, int(request.args.get("days",  10))))
    risk_param   = max(10.0, min(1000.0, float(request.args.get("risk", 100))))

    stocks = ([s.strip().upper() for s in stocks_param.split(",") if s.strip()]
              if stocks_param else DEFAULT_STOCKS)

    try:
        result = run_portfolio_backtest(stocks=stocks, days=days_param, risk=risk_param)
        result_dict = {
            "stocks":         result.stocks,
            "days":           result.days,
            "risk_per_trade": result.risk_per_trade,
            "start_date":     str(result.start_date),
            "end_date":       str(result.end_date),
            "mode_a":         result.mode_a,
            "mode_b":         result.mode_b,
            "trade_log": [
                {
                    "symbol":    r.symbol,
                    "date":      str(r.date),
                    "mode":      r.mode,
                    "direction": r.direction,
                    "entry":     r.entry,
                    "stop":      r.initial_stop,
                    "exit":      r.exit_price,
                    "reason":    r.exit_reason,
                    "pnl":       r.pnl,
                    "r":         r.r_multiple,
                    "qty":       r.qty,
                }
                for day in result.day_results
                for r in [day.mode_a, day.mode_b]
                if r is not None
            ],
            "generated_at": result.generated_at,
        }
        resp = make_response(jsonify(result_dict))
        resp.headers["Cache-Control"] = "public, s-maxage=300"
        return resp
    except Exception as e:
        return jsonify({"status": "ERROR", "reason": str(e)}), 500


# Vercel needs the `app` object at module level — nothing else needed
