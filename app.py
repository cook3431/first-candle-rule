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


# Vercel needs the `app` object at module level — nothing else needed
