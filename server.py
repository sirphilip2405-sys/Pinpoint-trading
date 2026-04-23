from flask import Flask, request, jsonify
import requests
import json
from datetime import datetime

app = Flask(__name__)

WATCHLIST = ["BTCUSDT", "ETHUSDT", "GBPUSDT", "EURUSDT", "XAUUSDT", "USDTJPY"]
ACCOUNT_BALANCE = 10
RISK_PCT = 0.01
ATR_MULTIPLIER = 1.5
MIN_RR = 3.0

webhook_cache = {}
live_signals = {}

def fetch_binance_ohlcv(symbol, interval, limit=100):
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if isinstance(data, dict) and data.get("code"):
            return fetch_forex_ohlcv(symbol, interval, limit)
        candles = []
        for d in data:
            candles.append({
                "open": float(d[1]),
                "high": float(d[2]),
                "low": float(d[3]),
                "close": float(d[4]),
                "volume": float(d[5])
            })
        return candles
    except Exception as e:
        return fetch_forex_ohlcv(symbol, interval, limit)

def fetch_forex_ohlcv(symbol, interval, limit=100):
    try:
        forex_map = {
            "EURUSDT": "EURUSD=X",
            "GBPUSDT": "GBPUSD=X",
            "XAUUSDT": "GC=F",
            "USDTJPY": "USDJPY=X"
        }
        tf_map = {
            "5m": "5m",
            "15m": "15m",
            "1h": "1h"
        }
        ticker = forex_map.get(symbol, symbol)
        interval_yf = tf_map.get(interval, "1h")
        period = "5d" if interval in ["5m", "15m"] else "30d"
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker
        params = {
            "interval": interval_yf,
            "range": period
        }
        headers = {
            "User-Agent": "Mozilla/5.0"
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        ohlcv = result["indicators"]["quote"][0]
        candles = []
        for i in range(len(timestamps)):
            try:
                candles.append({
                    "open":   float(ohlcv["open"][i]),
                    "high":   float(ohlcv["high"][i]),
                    "low":    float(ohlcv["low"][i]),
                    "close":  float(ohlcv["close"][i]),
                    "volume": float(ohlcv.get("volume", [1000]*len(timestamps))[i] or 1000)
                })
            except:
                continue
        return candles[-limit:] if len(candles) > limit else candles
    except Exception as e:
        return None

def compute_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i-1]["close"]
        tr = max(h-l, abs(h-pc), abs(l-pc))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs)/len(trs) if trs else 0
    return sum(trs[-period:]) / period

def compute_rsi(candles, period=14):
    closes = [c["close"] for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    if len(gains) < period:
        return 50
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def detect_structure(candles, lookback=10):
    if len(candles) < lookback * 2 + 2:
        return {"trend": "neutral", "bos": False,
                "choch": False, "swing_high": None, "swing_low": None}
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    recent_high = max(highs[-lookback:])
    recent_low = min(lows[-lookback:])
    prev_high = max(highs[-lookback*2:-lookback])
    prev_low = min(lows[-lookback*2:-lookback])
    close = candles[-1]["close"]
    bos = False
    choch = False
    trend = "neutral"
    if recent_high > prev_high and recent_low > prev_low:
        trend = "bullish"
        if close > recent_high:
            bos = True
    elif recent_high < prev_high and recent_low < prev_low:
        trend = "bearish"
        if close < recent_low:
            bos = True
    if prev_high < recent_high and close > recent_high:
        choch = True
        trend = "bullish"
    elif prev_low > recent_low and close < recent_low:
        choch = True
        trend = "bearish"
    return {
        "trend": trend,
        "bos": bos,
        "choch": choch,
        "swing_high": recent_high,
        "swing_low": recent_low
    }

def analyze(symbol):
    tf_map = {"M15": "15m", "M30": "30m", "H1": "1h"}
    tf_data = {}
    for tf, interval in tf_map.items():
        candles = fetch_binance_ohlcv(symbol, interval)
        if candles:
            tf_data[tf] = {
                "candles": candles,
                "atr": compute_atr(candles),
                "rsi": compute_rsi(candles),
                "structure": detect_structure(candles)
            }
    if len(tf_data) < 2:
        return {"symbol": symbol, "score": 0,
                "reason": "Not enough data", "signal": None}
    h1 = tf_data.get("H1", {})
    m5 = tf_data.get("M15", {})
    m15 = tf_data.get("M15", {})
    h1_trend = h1.get("structure", {}).get("trend", "neutral")
    m5_trend = m5.get("structure", {}).get("trend", "neutral")
    m15 = tf_data.get("M30", {})
m15_trend = m15.get("structure", {}).get("trend", "neutral") if m15 else "neutral"
    score = 0
    reasons = []
    direction = None
    if h1_trend == m5_trend and h1_trend != "neutral":
        score += 30
        direction = "long" if h1_trend == "bullish" else "short"
        reasons.append("H1+M5 confluent (" + h1_trend + ")")
    else:
        return {"symbol": symbol, "score": 0,
                "reason": "Trend mismatch H1=" + h1_trend + " M5=" + m5_trend,
                "signal": None}
    m5_struct = m5.get("structure", {})
    if m5_struct.get("choch"):
        score += 25
        reasons.append("M5 CHoCH")
    elif m5_struct.get("bos"):
        score += 20
        reasons.append("M5 BoS")
    h1_struct = h1.get("structure", {})
    if h1_struct.get("bos") or h1_struct.get("choch"):
        score += 15
        reasons.append("H1 structure confirmed")
    m5_rsi = m5.get("rsi", 50)
    if direction == "long" and 30 < m5_rsi < 65:
        score += 10
        reasons.append("RSI healthy (" + str(m5_rsi) + ")")
    elif direction == "short" and 35 < m5_rsi < 70:
        score += 10
        reasons.append("RSI healthy (" + str(m5_rsi) + ")")
    if m15_trend == h1_trend:
        score += 10
        reasons.append("M15 confirms")
    entry = m5["candles"][-1]["close"]
    atr = m5.get("atr", entry * 0.001)
    swing_high = m5_struct.get("swing_high")
    swing_low = m5_struct.get("swing_low")
    if direction == "long":
        sl_atr = entry - ATR_MULTIPLIER * atr
        sl_swing = swing_low if swing_low and swing_low < entry else sl_atr
        stop_loss = max(sl_atr, sl_swing)
    else:
        sl_atr = entry + ATR_MULTIPLIER * atr
        sl_swing = swing_high if swing_high and swing_high > entry else sl_atr
        stop_loss = min(sl_atr, sl_swing)
    risk = abs(entry - stop_loss)
    if risk < 0.000001:
        return {"symbol": symbol, "score": score,
                "reason": "Invalid SL", "signal": None}
    tp = entry + risk * MIN_RR if direction == "long" else entry - risk * MIN_RR
    size = round((ACCOUNT_BALANCE * RISK_PCT) / risk, 4)
    signal = {
        "symbol": symbol,
        "direction": direction,
        "entry": round(entry, 5),
        "stop_loss": round(stop_loss, 5),
        "take_profit": round(tp, 5),
        "rr_ratio": MIN_RR,
        "position_size": size,
        "risk_usd": round(ACCOUNT_BALANCE * RISK_PCT, 2),
        "score": round(score, 1),
        "reason": " | ".join(reasons),
        "timeframes": {
            tf: {
                "trend": d.get("structure", {}).get("trend"),
                "rsi": d.get("rsi")
            }
            for tf, d in tf_data.items()
        }
    }
    live_signals[symbol] = signal
    return {
        "symbol": symbol,
        "score": round(score, 1),
        "direction": direction,
        "reason": " | ".join(reasons),
        "signal": signal
    }

@app.route("/health")
def health():
    return jsonify({
        "status": "running",
        "time": datetime.now().isoformat(),
        "watchlist": WATCHLIST
    })

@app.route("/scan")
def scan():
    results = []
    for symbol in WATCHLIST:
        try:
            results.append(analyze(symbol))
        except Exception as e:
            results.append({"symbol": symbol, "score": 0, "reason": str(e)})
    results.sort(key=lambda x: x["score"], reverse=True)
    return jsonify(results)

@app.route("/analyze/<symbol>")
def analyze_symbol(symbol):
    return jsonify(analyze(symbol.upper()))

@app.route("/signals")
def signals():
    return jsonify(live_signals)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    symbol = data.get("symbol", "").upper()
    tf = data.get("timeframe", "").upper()
    if symbol and tf:
        if symbol not in webhook_cache:
            webhook_cache[symbol] = {}
        webhook_cache[symbol][tf] = data
    return jsonify({"status": "ok"})

@app.route("/dashboard")
def dashboard():
    results = []
    for symbol in WATCHLIST:
        try:
            results.append(analyze(symbol))
        except Exception as e:
            results.append({"symbol": symbol, "score": 0, "reason": str(e), "signal": None})
    results.sort(key=lambda x: x["score"], reverse=True)
    rows = ""
    for r in results:
        score = r.get("score", 0)
        symbol = r.get("symbol", "")
        reason = r.get("reason", "")
        direction = r.get("direction", "-")
        signal = r.get("signal") or {}
        entry = signal.get("entry", "-")
        sl = signal.get("stop_loss", "-")
        tp = signal.get("take_profit", "-")
        size = signal.get("position_size", "-")
        if score >= 70:
            color = "#00ff88"
            emoji = "🟢"
        elif score >= 40:
            color = "#ffaa00"
            emoji = "🟡"
        else:
            color = "#ff4444"
            emoji = "🔴"
        dir_color = "#00ff88" if direction == "long" else "#ff4444" if direction == "short" else "#888"
        reason_short = reason[:50] + "..." if len(reason) > 50 else reason
        rows += "<tr>"
        rows += "<td><b style='color:#fff'>" + symbol + "</b></td>"
        rows += "<td><span style='color:" + color + ";font-weight:bold'>" + emoji + " " + str(score) + "</span></td>"
        rows += "<td><span style='color:" + dir_color + "'>" + (direction.upper() if direction and direction != "-" else "-") + "</span></td>"
        rows += "<td style='color:#ccc'>" + str(entry) + "</td>"
        rows += "<td style='color:#ff6b6b'>" + str(sl) + "</td>"
        rows += "<td style='color:#00ff88'>" + str(tp) + "</td>"
        rows += "<td style='color:#aaa'>" + str(size) + "</td>"
        rows += "<td style='color:#666;font-size:0.8em'>" + reason_short + "</td>"
        rows += "</tr>"
    html = """<!DOCTYPE html>
<html>
<head>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Pinpoint Trading</title>
<meta http-equiv='refresh' content='30'>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0a0a0f; color: #fff; font-family: monospace; padding: 10px; }
h1 { color: #00ff88; text-align: center; padding: 15px 0; font-size: 1.4em; letter-spacing: 2px; }
.sub { text-align: center; color: #555; font-size: 0.8em; margin-bottom: 15px; }
table { width: 100%; border-collapse: collapse; font-size: 0.75em; }
th { background: #111; color: #00ff88; padding: 8px 4px; text-align: left; border-bottom: 1px solid #222; }
td { padding: 8px 4px; border-bottom: 1px solid #111; vertical-align: middle; }
tr:hover { background: #111; }
.links { display: flex; gap: 8px; justify-content: center; margin: 10px 0; flex-wrap: wrap; }
.links a { color: #00ff88; text-decoration: none; border: 1px solid #00ff88; padding: 5px 10px; border-radius: 4px; font-size: 0.8em; }
.footer { text-align: center; color: #333; font-size: 0.7em; margin-top: 15px; }
</style>
</head>
<body>
<h1>PHILIP'S TRADE DESK</h1>
<p class='sub'>""" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + """ · Auto-refresh 30s</p>
<div class='links'>
<a href='/dashboard'>Refresh</a>
<a href='/scan'>JSON</a>
<a href='/health'>Health</a>
</div>
<table>
<tr>
<th>Symbol</th><th>Score</th><th>Dir</th>
<th>Entry</th><th>SL</th><th>TP</th>
<th>Size</th><th>Reason</th>
</tr>""" + rows + """
</table>
<p class='footer'>Auto-refreshes every 30 seconds</p>
</body>
</html>"""
    return html

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
