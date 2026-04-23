from flask import Flask, request, jsonify
import requests
import json
from datetime import datetime

app = Flask(__name__)

WATCHLIST = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
ACCOUNT_BALANCE = 10000
RISK_PCT = 0.01
ATR_MULTIPLIER = 1.5
MIN_RR = 3.0

webhook_cache = {}
live_signals = {}

def fetch_binance_ohlcv(symbol, interval, limit=100):
    try:
        url = f"https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
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
    if prev_high < prev_high and close > recent_high:
        choch = True
        trend = "bullish"
    elif prev_low > prev_low and close < recent_low:
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
    tf_map = {"M5": "5m", "M15": "15m", "H1": "1h"}
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
    m5 = tf_data.get("M5", {})
    m15 = tf_data.get("M15", {})
    h1_trend = h1.get("structure", {}).get("trend", "neutral")
    m5_trend = m5.get("structure", {}).get("trend", "neutral")
    m15_trend = m15.get("structure", {}).get("trend", "neutral") if m15 else "neutral"
    score = 0
    reasons = []
    direction = None
    if h1_trend == m5_trend and h1_trend != "neutral":
        score += 30
        direction = "long" if h1_trend == "bullish" else "short"
        reasons.append(f"H1+M5 trend confluent ({h1_trend})")
    else:
        return {"symbol": symbol, "score": 0,
                "reason": f"Trend mismatch H1={h1_trend} M5={m5_trend}",
                "signal": None}
    m5_struct = m5.get("structure", {})
    if m5_struct.get("choch"):
        score += 25
        reasons.append("M5 CHoCH confirmed")
    elif m5_struct.get("bos"):
        score += 20
        reasons.append("M5 BoS confirmed")
    h1_struct = h1.get("structure", {})
    if h1_struct.get("bos") or h1_struct.get("choch"):
        score += 15
        reasons.append("H1 structure confirmed")
    m5_rsi = m5.get("rsi", 50)
    if direction == "long" and 30 < m5_rsi < 65:
        score += 10
        reasons.append(f"RSI healthy ({m5_rsi})")
    elif direction == "short" and 35 < m5_rsi < 70:
        score += 10
        reasons.append(f"RSI healthy ({m5_rsi})")
    if m15_trend == h1_trend:
        score += 10
        reasons.append("M15 confirms trend")
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
    return {"symbol": symbol, "score": round(score, 1),
            "direction": direction, "reason": " | ".join(reasons),
            "signal": signal}

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
