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
    Body JSON: { symbol, direction, entry, stop_loss, take_profit }
    Risk is fixed at $100 per trade (qty calculated server-side).
    """
    from broker import execute_bracket_order
    data = request.get_json(silent=True) or {}

    symbol      = str(data.get("symbol",      "")).upper().strip()
    direction   = str(data.get("direction",   "")).upper().strip()
    entry       = float(data.get("entry",       0) or 0)
    stop_loss   = float(data.get("stop_loss",   0) or 0)
    take_profit = float(data.get("take_profit", 0) or 0)

    if not symbol or direction not in ("LONG", "SHORT") or not entry or not stop_loss or not take_profit:
        return jsonify({"success": False, "error": "Missing or invalid parameters."}), 400

    result = execute_bracket_order(symbol, direction, entry, stop_loss, take_profit, risk_dollars=100.0)
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


# Vercel needs the `app` object at module level — nothing else needed
