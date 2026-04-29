from flask import Flask, request, jsonify, redirect
import requests
from datetime import datetime, timezone, timedelta
import os
import threading
import time

app = Flask(__name__)

WATCHLIST = ["BTCUSDT", "ETHUSDT", "GBPUSDT", "EURUSDT", "XAUUSDT", "USDTJPY"]
ACCOUNT_BALANCE = 10
RISK_PCT = 0.01
ATR_MULTIPLIER = 1.5
MIN_RR = 3.0
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

webhook_cache = {}
live_signals = {}
trade_log = []
active_trades = {}
signal_history = {}
notified_signals = set()

# ─── NEWS CALENDAR ───────────────────────────────────────────
NEWS_EVENTS = [
    {"name": "NFP (Non-Farm Payrolls)", "day": 4, "hour": 15, "minute": 30, "pairs": ["EURUSDT", "GBPUSDT", "XAUUSDT", "USDTJPY"]},
    {"name": "US CPI", "day": 1, "hour": 15, "minute": 30, "pairs": ["EURUSDT", "GBPUSDT", "XAUUSDT", "USDTJPY"]},
    {"name": "Fed Rate Decision", "day": 2, "hour": 21, "minute": 0, "pairs": ["EURUSDT", "GBPUSDT", "XAUUSDT", "USDTJPY", "BTCUSDT", "ETHUSDT"]},
    {"name": "BOE Rate Decision", "day": 3, "hour": 14, "minute": 0, "pairs": ["GBPUSDT"]},
    {"name": "ECB Rate Decision", "day": 3, "hour": 14, "minute": 15, "pairs": ["EURUSDT"]},
    {"name": "US Retail Sales", "day": 1, "hour": 15, "minute": 30, "pairs": ["EURUSDT", "GBPUSDT", "XAUUSDT"]},
    {"name": "US GDP", "day": 3, "hour": 15, "minute": 30, "pairs": ["EURUSDT", "GBPUSDT", "XAUUSDT", "USDTJPY"]},
    {"name": "FOMC Minutes", "day": 2, "hour": 21, "minute": 0, "pairs": ["EURUSDT", "GBPUSDT", "XAUUSDT", "USDTJPY"]},
    {"name": "BOJ Rate Decision", "day": 4, "hour": 3, "minute": 0, "pairs": ["USDTJPY"]},
    {"name": "US PPI", "day": 1, "hour": 15, "minute": 30, "pairs": ["EURUSDT", "GBPUSDT", "XAUUSDT"]},
]

CORRELATIONS = {
    "EURUSDT": ["GBPUSDT"],
    "GBPUSDT": ["EURUSDT"],
    "XAUUSDT": ["BTCUSDT"],
    "BTCUSDT": ["ETHUSDT", "XAUUSDT"],
    "ETHUSDT": ["BTCUSDT"],
    "USDTJPY": [],
}

def get_eat_time():
    return datetime.now(timezone.utc) + timedelta(hours=3)

def is_market_session():
    now = get_eat_time()
    hour = now.hour
    weekday = now.weekday()
    if weekday >= 5:
        return False, "Weekend - markets closed"
    if 10 <= hour < 13:
        return True, "London Session"
    if 16 <= hour < 20:
        return True, "New York Session"
    if 10 <= hour < 20:
        return True, "London/NY Overlap"
    return False, "Off-session hours"

def check_news_filter(symbol):
    now = get_eat_time()
    current_weekday = now.weekday()
    current_hour = now.hour
    current_minute = now.minute
    current_total = current_hour * 60 + current_minute
    for event in NEWS_EVENTS:
        if symbol not in event["pairs"]:
            continue
        if current_weekday != event["day"]:
            continue
        event_total = event["hour"] * 60 + event["minute"]
        if abs(current_total - event_total) <= 30:
            return True, event["name"]
    return False, None

def check_correlations(symbol, direction):
    warnings = []
    correlated = CORRELATIONS.get(symbol, [])
    for sym in correlated:
        if sym in active_trades:
            active_dir = active_trades[sym].get("direction")
            if active_dir == direction:
                warnings.append(sym + " already " + direction)
    return warnings

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except:
        pass

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
    except:
        return fetch_forex_ohlcv(symbol, interval, limit)

def fetch_forex_ohlcv(symbol, interval, limit=100):
    try:
        forex_map = {
            "EURUSDT": "EURUSD=X",
            "GBPUSDT": "GBPUSD=X",
            "XAUUSDT": "GC=F",
            "USDTJPY": "USDJPY=X"
        }
        tf_map = {"15m": "15m", "30m": "30m", "1h": "1h"}
        ticker = forex_map.get(symbol, symbol)
        interval_yf = tf_map.get(interval, "1h")
        period = "5d" if interval in ["15m", "30m"] else "30d"
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker
        params = {"interval": interval_yf, "range": period}
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        ohlcv = result["indicators"]["quote"][0]
        candles = []
        for i in range(len(timestamps)):
            try:
                candles.append({
                    "open": float(ohlcv["open"][i]),
                    "high": float(ohlcv["high"][i]),
                    "low": float(ohlcv["low"][i]),
                    "close": float(ohlcv["close"][i]),
                    "volume": float(ohlcv.get("volume", [1000]*len(timestamps))[i] or 1000)
                })
            except:
                continue
        return candles[-limit:] if len(candles) > limit else candles
    except:
        return None

def get_live_price(symbol):
    try:
        if symbol in ["EURUSDT", "GBPUSDT", "XAUUSDT", "USDTJPY"]:
            forex_map = {
                "EURUSDT": "EURUSD=X",
                "GBPUSDT": "GBPUSD=X",
                "XAUUSDT": "GC=F",
                "USDTJPY": "USDJPY=X"
            }
            ticker = forex_map.get(symbol)
            url = "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker
            params = {"interval": "1m", "range": "1d"}
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, params=params, headers=headers, timeout=10)
            data = r.json()
            return float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
        else:
            url = "https://api.binance.com/api/v3/ticker/price"
            r = requests.get(url, params={"symbol": symbol}, timeout=10)
            return float(r.json()["price"])
    except:
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

def calculate_pips(symbol, entry, exit_price, direction):
    if symbol in ["USDTJPY"]:
        multiplier = 100
    elif symbol in ["XAUUSDT"]:
        multiplier = 10
    elif symbol in ["BTCUSDT", "ETHUSDT"]:
        multiplier = 1
    else:
        multiplier = 10000
    if direction == "long":
        pips = (exit_price - entry) * multiplier
    else:
        pips = (entry - exit_price) * multiplier
    return round(pips, 1)

def analyze(symbol):
    news_blocked, news_name = check_news_filter(symbol)
    if news_blocked:
        return {"symbol": symbol, "score": 0,
                "reason": "NEWS BLOCK: " + news_name, "signal": None}
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
    if len(tf_data) < 1:
        return {"symbol": symbol, "score": 0,
                "reason": "Not enough data", "signal": None}
    h1 = tf_data.get("H1", {})
    m15 = tf_data.get("M15", {})
    m30 = tf_data.get("M30", {})
    h1_trend = h1.get("structure", {}).get("trend", "neutral")
    m15_trend = m15.get("structure", {}).get("trend", "neutral")
    m30_trend = m30.get("structure", {}).get("trend", "neutral") if m30 else "neutral"
    score = 0
    reasons = []
    direction = None
    if h1_trend == m15_trend and h1_trend != "neutral":
        score += 30
        direction = "long" if h1_trend == "bullish" else "short"
        reasons.append("H1+M15 confluent (" + h1_trend + ")")
    else:
        return {"symbol": symbol, "score": 0,
                "reason": "Trend mismatch H1=" + h1_trend + " M15=" + m15_trend,
                "signal": None}
    m15_struct = m15.get("structure", {})
    if m15_struct.get("choch"):
        score += 25
        reasons.append("M15 CHoCH")
    elif m15_struct.get("bos"):
        score += 20
        reasons.append("M15 BoS")
    h1_struct = h1.get("structure", {})
    if h1_struct.get("bos") or h1_struct.get("choch"):
        score += 15
        reasons.append("H1 structure confirmed")
    m15_rsi = m15.get("rsi", 50)
    if direction == "long" and 30 < m15_rsi < 65:
        score += 10
        reasons.append("RSI healthy (" + str(m15_rsi) + ")")
    elif direction == "short" and 35 < m15_rsi < 70:
        score += 10
        reasons.append("RSI healthy (" + str(m15_rsi) + ")")
    if m30_trend == h1_trend:
        score += 10
        reasons.append("M30 confirms")
    entry = m15["candles"][-1]["close"]
    atr = m15.get("atr", entry * 0.001)
    m15_swing_high = m15_struct.get("swing_high")
    m15_swing_low = m15_struct.get("swing_low")
    if direction == "long":
        sl_atr = entry - ATR_MULTIPLIER * atr
        sl_swing = m15_swing_low if m15_swing_low and m15_swing_low < entry else sl_atr
        stop_loss = max(sl_atr, sl_swing)
    else:
        sl_atr = entry + ATR_MULTIPLIER * atr
        sl_swing = m15_swing_high if m15_swing_high and m15_swing_high > entry else sl_atr
        stop_loss = min(sl_atr, sl_swing)
    risk = abs(entry - stop_loss)
    if risk < 0.000001:
        return {"symbol": symbol, "score": score,
                "reason": "Invalid SL", "signal": None}
    tp = entry + risk * MIN_RR if direction == "long" else entry - risk * MIN_RR
    size = round((ACCOUNT_BALANCE * RISK_PCT) / risk, 4)
    corr_warnings = check_correlations(symbol, direction)
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
        "correlation_warning": corr_warnings,
        "timeframes": {
            tf: {
                "trend": d.get("structure", {}).get("trend"),
                "rsi": d.get("rsi")
            }
            for tf, d in tf_data.items()
        }
    }
    live_signals[symbol] = signal
    if symbol not in active_trades:
        active_trades[symbol] = signal
    if symbol not in signal_history:
        signal_history[symbol] = []
    signal_history[symbol].append({
        "time": get_eat_time().strftime("%H:%M"),
        "score": round(score, 1),
        "direction": direction
    })
    signal_history[symbol] = signal_history[symbol][-20:]
    signal_key = symbol + "_" + str(round(score, 1)) + "_" + direction
    if score >= 70 and signal_key not in notified_signals:
        notified_signals.add(signal_key)
        corr_text = ""
        if corr_warnings:
            corr_text = "\n⚠️ Correlation: " + ", ".join(corr_warnings)
        msg = (
            "🎯 <b>PINPOINT SIGNAL</b>\n\n"
            "📊 <b>" + symbol + "</b> — " + direction.upper() + "\n"
            "Score: " + str(round(score, 1)) + "/100\n\n"
            "Entry: " + str(round(entry, 5)) + "\n"
            "SL: " + str(round(stop_loss, 5)) + "\n"
            "TP: " + str(round(tp, 5)) + "\n"
            "RR: 1:" + str(MIN_RR) + "\n"
            "Size: " + str(size) + "\n\n"
            "📝 " + " | ".join(reasons) +
            corr_text
        )
        send_telegram(msg)
    return {
        "symbol": symbol,
        "score": round(score, 1),
        "direction": direction,
        "reason": " | ".join(reasons),
        "correlation_warning": corr_warnings,
        "signal": signal
    }

def auto_check_sl_tp():
    while True:
        try:
            for symbol in list(active_trades.keys()):
                trade = active_trades[symbol]
                price = get_live_price(symbol)
                if not price:
                    continue
                entry = trade.get("entry", 0)
                sl = trade.get("stop_loss", 0)
                tp = trade.get("take_profit", 0)
                direction = trade.get("direction", "")
                hit = None
                if direction == "long":
                    if price >= tp:
                        hit = "win"
                    elif price <= sl:
                        hit = "loss"
                elif direction == "short":
                    if price <= tp:
                        hit = "win"
                    elif price >= sl:
                        hit = "loss"
                if hit:
                    now = get_eat_time()
                    exit_price = tp if hit == "win" else sl
                    pips = calculate_pips(symbol, entry, exit_price, direction)
                    profit_usd = round(pips * (ACCOUNT_BALANCE * RISK_PCT / abs(entry - sl)) if abs(entry - sl) > 0 else 0, 2)
                    trade_log.append({
                        "time": now.strftime("%H:%M"),
                        "date": now.strftime("%Y-%m-%d"),
                        "symbol": symbol,
                        "direction": direction,
                        "entry": entry,
                        "exit": round(exit_price, 5),
                        "stop_loss": sl,
                        "take_profit": tp,
                        "score": trade.get("score", 0),
                        "result": hit,
                        "pips": pips,
                        "profit_usd": profit_usd,
                        "auto": True
                    })
                    del active_trades[symbol]
                    emoji = "✅" if hit == "win" else "❌"
                    send_telegram(
                        emoji + " <b>TRADE CLOSED (AUTO)</b>\n\n"
                        "📊 <b>" + symbol + "</b> — " + direction.upper() + "\n"
                        "Result: <b>" + hit.upper() + "</b>\n"
                        "Entry: " + str(entry) + "\n"
                        "Exit: " + str(round(exit_price, 5)) + "\n"
                        "Pips: " + str(pips) + "\n"
                        "P&L: $" + str(profit_usd)
                    )
        except Exception as e:
            pass
        time.sleep(300)

def daily_summary():
    while True:
        try:
            now = get_eat_time()
            if now.hour == 18 and now.minute < 5:
                wins = len([t for t in trade_log if t["result"] == "win"])
                losses = len([t for t in trade_log if t["result"] == "loss"])
                total = wins + losses
                winrate = round((wins / total * 100)) if total > 0 else 0
                total_pips = sum([t.get("pips", 0) for t in trade_log])
                total_profit = sum([t.get("profit_usd", 0) for t in trade_log])
                best = max(trade_log, key=lambda x: x.get("pips", 0)) if trade_log else None
                worst = min(trade_log, key=lambda x: x.get("pips", 0)) if trade_log else None
                msg = (
                    "📊 <b>DAILY SUMMARY</b> — " + now.strftime("%Y-%m-%d") + "\n\n"
                    "✅ Wins: " + str(wins) + "\n"
                    "❌ Losses: " + str(losses) + "\n"
                    "📈 Win Rate: " + str(winrate) + "%\n"
                    "💰 Total Pips: " + str(round(total_pips, 1)) + "\n"
                    "💵 P&L: $" + str(round(total_profit, 2)) + "\n"
                )
                if best:
                    msg += "\n🏆 Best: " + best["symbol"] + " +" + str(best.get("pips", 0)) + " pips"
                if worst:
                    msg += "\n💔 Worst: " + worst["symbol"] + " " + str(worst.get("pips", 0)) + " pips"
                send_telegram(msg)
        except:
            pass
        time.sleep(60)

threading.Thread(target=auto_check_sl_tp, daemon=True).start()
threading.Thread(target=daily_summary, daemon=True).start()

@app.route("/health")
def health():
    session_active, session_name = is_market_session()
    return jsonify({
        "status": "running",
        "time_eat": get_eat_time().strftime("%Y-%m-%d %H:%M:%S"),
        "session": session_name,
        "session_active": session_active,
        "watchlist": WATCHLIST,
        "active_trades": len(active_trades),
        "telegram_configured": bool(TELEGRAM_TOKEN)
    })

@app.route("/scan")
def scan():
    results = []
    for symbol in WATCHLIST:
    try:
        result = analyze(symbol)
        if result:
            results.append(result)
        else:
            results.append({"symbol": symbol, "score": 0, "reason": "No data", "signal": None})
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
    return
