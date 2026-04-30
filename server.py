from flask import Flask, request, jsonify, redirect
import requests
from datetime import datetime, timezone, timedelta
import os

app = Flask(__name__)

WATCHLIST = ["BTCUSDT", "ETHUSDT", "GBPUSDT", "EURUSDT", "XAUUSDT", "USDTJPY"]
ACCOUNT_BALANCE = 10000
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

NEWS_EVENTS = [
    {"name": "NFP", "day": 4, "hour": 15, "minute": 30, "pairs": ["EURUSDT", "GBPUSDT", "XAUUSDT", "USDTJPY"]},
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
    if 13 <= hour < 16:
        return True, "London/NY Overlap"
    return False, "Off-session hours"

def check_news_filter(symbol):
    now = get_eat_time()
    current_weekday = now.weekday()
    current_total = now.hour * 60 + now.minute
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
    for sym in CORRELATIONS.get(symbol, []):
        if sym in active_trades:
            if active_trades[sym].get("direction") == direction:
                warnings.append(sym)
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
            ticker = forex_map[symbol]
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
    if symbol == "USDTJPY":
        multiplier = 100
    elif symbol == "XAUUSDT":
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

def check_sl_tp_hits():
    for symbol in list(active_trades.keys()):
        try:
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
                risk_dist = abs(entry - sl)
                profit_usd = round(pips * (ACCOUNT_BALANCE * RISK_PCT / risk_dist) if risk_dist > 0 else 0, 2)
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
                    emoji + " <b>AUTO CLOSED</b>\n"
                    "📊 <b>" + symbol + "</b> " + direction.upper() + "\n"
                    "Result: <b>" + hit.upper() + "</b>\n"
                    "Pips: " + str(pips) + " | P&L: $" + str(profit_usd)
                )
        except:
            continue

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
            corr_text = "\n⚠️ Correlated: " + ", ".join(corr_warnings)
        send_telegram(
            "🎯 <b>PINPOINT SIGNAL</b>\n\n"
            "📊 <b>" + symbol + "</b> — " + direction.upper() + "\n"
            "Score: " + str(round(score, 1)) + "/100\n\n"
            "Entry: " + str(round(entry, 5)) + "\n"
            "SL: " + str(round(stop_loss, 5)) + "\n"
            "TP: " + str(round(tp, 5)) + "\n"
            "RR: 1:" + str(MIN_RR) + "\n"
            "Size: " + str(size) + "\n\n"
            "📝 " + " | ".join(reasons) + corr_text
        )
    return {
        "symbol": symbol,
        "score": round(score, 1),
        "direction": direction,
        "reason": " | ".join(reasons),
        "correlation_warning": corr_warnings,
        "signal": signal
    }

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

@app.route("/close_trade")
def close_trade():
    symbol = request.args.get("symbol", "").upper()
    result = request.args.get("result", "")
    if symbol in active_trades and result in ["win", "loss"]:
        t = active_trades[symbol]
        now = get_eat_time()
        entry = t.get("entry", 0)
        sl = t.get("stop_loss", 0)
        exit_price = t.get("take_profit", 0) if result == "win" else sl
        direction = t.get("direction", "")
        pips = calculate_pips(symbol, entry, exit_price, direction)
        risk_dist = abs(entry - sl)
        profit_usd = round(pips * (ACCOUNT_BALANCE * RISK_PCT / risk_dist) if risk_dist > 0 else 0, 2)
        trade_log.append({
            "time": now.strftime("%H:%M"),
            "date": now.strftime("%Y-%m-%d"),
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "exit": round(exit_price, 5),
            "stop_loss": sl,
            "take_profit": t.get("take_profit", 0),
            "score": t.get("score", 0),
            "result": result,
            "pips": pips,
            "profit_usd": profit_usd,
            "auto": False
        })
        del active_trades[symbol]
        emoji = "✅" if result == "win" else "❌"
        send_telegram(
            emoji + " <b>TRADE CLOSED</b>\n"
            "📊 <b>" + symbol + "</b> " + direction.upper() + "\n"
            "Result: <b>" + result.upper() + "</b>\n"
            "Pips: " + str(pips) + " | P&L: $" + str(profit_usd)
        )
    return redirect("/dashboard")

@app.route("/cancel_trade")
def cancel_trade():
    symbol = request.args.get("symbol", "").upper()
    if symbol in active_trades:
        del active_trades[symbol]
    return redirect("/dashboard")

@app.route("/clearlog")
def clear_log():
    trade_log.clear()
    notified_signals.clear()
    return redirect("/dashboard")

@app.route("/weekly")
def weekly():
    now = get_eat_time()
    by_pair = {}
    by_day = {}
    for t in trade_log:
        sym = t["symbol"]
        if sym not in by_pair:
            by_pair[sym] = {"wins": 0, "losses": 0, "pips": 0}
        by_pair[sym]["wins" if t["result"] == "win" else "losses"] += 1
        by_pair[sym]["pips"] += t.get("pips", 0)
        day = t.get("date", "unknown")
        if day not in by_day:
            by_day[day] = {"wins": 0, "losses": 0}@app.route("/dashboard")
def dashboard():
    try:
        check_sl_tp_hits()
    except:
        pass
    results = []
    for symbol in WATCHLIST:
        try:
            results.append(analyze(symbol))
        except Exception as e:
            results.append({"symbol": symbol, "score": 0, "reason": str(e), "signal": None})
    results.sort(key=lambda x: x["score"], reverse=True)
    session_active, session_name = is_market_session()
    session_color = "#00ff88" if session_active else "#ff4444"
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
        corr = r.get("correlation_warning", [])
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
        reason_short = reason[:45] + "..." if len(reason) > 45 else reason
        corr_text = " ⚠️" if corr else ""
        rows += "<tr>"
        rows += "<td><b style='color:#fff'>" + symbol + "</b></td>"
        rows += "<td><span style='color:" + color + ";font-weight:bold'>" + emoji + " " + str(score) + "</span></td>"
        rows += "<td><span style='color:" + dir_color + "'>" + (direction.upper() if direction and direction != "-" else "-") + "</span></td>"
        rows += "<td style='color:#ccc'>" + str(entry) + "</td>"
        rows += "<td style='color:#ff6b6b'>" + str(sl) + "</td>"
        rows += "<td style='color:#00ff88'>" + str(tp) + "</td>"
        rows += "<td style='color:#aaa'>" + str(size) + "</td>"
        rows += "<td style='color:#666;font-size:0.8em'>" + reason_short + corr_text + "</td>"
        rows += "</tr>"
    active_rows = ""
    if active_trades:
        for sym, t in active_trades.items():
            dir_color = "#00ff88" if t.get("direction") == "long" else "#ff4444"
            corr = t.get("correlation_warning", [])
            corr_badge = " ⚠️" if corr else ""
            active_rows += "<tr>"
            active_rows += "<td style='color:#fff'>" + sym + corr_badge + "</td>"
            active_rows += "<td style='color:" + dir_color + "'>" + (t.get("direction") or "").upper() + "</td>"
            active_rows += "<td style='color:#ccc'>" + str(t.get("entry", "-")) + "</td>"
            active_rows += "<td style='color:#ff6b6b'>" + str(t.get("stop_loss", "-")) + "</td>"
            active_rows += "<td style='color:#00ff88'>" + str(t.get("take_profit", "-")) + "</td>"
            active_rows += "<td style='color:#ffaa00'>" + str(t.get("score", "-")) + "</td>"
            active_rows += "<td>"
            active_rows += "<a href='/close_trade?symbol=" + sym + "&result=win' style='color:#00ff88;text-decoration:none;border:1px solid #00ff88;padding:2px 5px;border-radius:3px;font-size:0.75em;margin-right:3px;'>WIN</a>"
            active_rows += "<a href='/close_trade?symbol=" + sym + "&result=loss' style='color:#ff4444;text-decoration:none;border:1px solid #ff4444;padding:2px 5px;border-radius:3px;font-size:0.75em;margin-right:3px;'>LOSS</a>"
            active_rows += "<a href='/cancel_trade?symbol=" + sym + "' style='color:#888;text-decoration:none;border:1px solid #444;padding:2px 5px;border-radius:3px;font-size:0.75em;'>X</a>"
            active_rows += "</td></tr>"
    else:
        active_rows = "<tr><td colspan='7' style='color:#444;text-align:center;padding:15px;'>No active trades</td></tr>"
    wins = len([t for t in trade_log if t["result"] == "win"])
    losses = len([t for t in trade_log if t["result"] == "loss"])
    total = wins + losses
    winrate = round((wins / total * 100)) if total > 0 else 0
    total_pips = round(sum([t.get("pips", 0) for t in trade_log]), 1)
    total_profit = round(sum([t.get("profit_usd", 0) for t in trade_log]), 2)
    pips_color = "#00ff88" if total_pips >= 0 else "#ff4444"
    profit_color = "#00ff88" if total_profit >= 0 else "#ff4444"
    trade_log_rows = ""
    for t in reversed(trade_log):
        res_color = "#00ff88" if t["result"] == "win" else "#ff4444"
        dir_color = "#00ff88" if t["direction"] == "long" else "#ff4444"
        pip_color = "#00ff88" if t.get("pips", 0) >= 0 else "#ff4444"
        auto_badge = " 🤖" if t.get("auto") else ""
        trade_log_rows += "<tr>"
        trade_log_rows += "<td style='color:#888'>" + t["time"] + "</td>"
        trade_log_rows += "<td style='color:#777'>" + t["date"] + "</td>"
        trade_log_rows += "<td style='color:#fff'>" + t["symbol"] + "</td>"
        trade_log_rows += "<td style='color:" + dir_color + "'>" + t["direction"].upper() + "</td>"
        trade_log_rows += "<td style='color:#ccc'>" + str(t["entry"]) + "</td>"
        trade_log_rows += "<td style='color:#aaa'>" + str(t.get("exit", "-")) + "</td>"
        trade_log_rows += "<td style='color:" + pip_color + "'>" + str(t.get("pips", "-")) + "</td>"
        trade_log_rows += "<td style='color:" + res_color + ";font-weight:bold'>" + ("WIN" if t["result"] == "win" else "LOSS") + auto_badge + "</td>"
        trade_log_rows += "</tr>"
    if not trade_log:
        trade_log_rows = "<tr><td colspan='8' style='color:#444;text-align:center;padding:20px;'>No trades logged yet</td></tr>"
    history_bars = ""
    for sym in WATCHLIST:
        history = signal_history.get(sym, [])
        if history:
            last = history[-1]
            bar_color = "#00ff88" if last["score"] >= 70 else "#ffaa00" if last["score"] >= 40 else "#ff4444"
            bar_width = str(int(last["score"])) + "%"
            history_bars += "<div style='margin-bottom:8px;'>"
            history_bars += "<div style='display:flex;justify-content:space-between;color:#888;font-size:0.75em;margin-bottom:2px;'>"
            history_bars += "<span>" + sym + "</span><span style='color:" + bar_color + "'>" + str(last["score"]) + "</span></div>"
            history_bars += "<div style='background:#111;border-radius:3px;height:8px;'>"
            history_bars += "<div style='background:" + bar_color + ";width:" + bar_width + ";height:8px;border-radius:3px;'></div>"
            history_bars += "</div></div>"
    if not history_bars:
        history_bars = "<p style='color:#444;text-align:center;padding:10px;font-size:0.8em;'>No signal history yet</p>"
    news_rows = ""
    now_eat = get_eat_time()
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for event in NEWS_EVENTS:
        day_name = days[event["day"]]
        time_str = str(event["hour"]).zfill(2) + ":" + str(event["minute"]).zfill(2)
        pairs_str = ", ".join([p.replace("USDT", "") for p in event["pairs"]])
        is_today = now_eat.weekday() == event["day"]
        row_color = "#ffaa00" if is_today else "#555"
        news_rows += "<tr>"
        news_rows += "<td style='color:" + row_color + "'>" + day_name + "</td>"
        news_rows += "<td style='color:" + row_color + "'>" + time_str + " EAT</td>"
        news_rows += "<td style='color:#fff'>" + event["name"] + "</td>"
        news_rows += "<td style='color:#888;font-size:0.8em'>" + pairs_str + "</td>"
        news_rows += "</tr>"
    now = get_eat_time()
    html = """<!DOCTYPE html>
<html>
<head>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Philip's Trade Desk</title>
<meta http-equiv='refresh' content='30'>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0a0a0f; color: #fff; font-family: monospace; padding: 10px; }
h1 { color: #00ff88; text-align: center; padding: 15px 0; font-size: 1.4em; letter-spacing: 2px; }
h2 { color: #00ff88; text-align: center; margin-top: 25px; margin-bottom: 8px; font-size: 1.0em; letter-spacing: 2px; }
.sub { text-align: center; color: #555; font-size: 0.8em; margin-bottom: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 0.72em; }
th { background: #111; color: #00ff88; padding: 7px 3px; text-align: left; border-bottom: 1px solid #222; }
td { padding: 7px 3px; border-bottom: 1px solid #111; vertical-align: middle; }
tr:hover { background: #111; }
.links { display: flex; gap: 8px; justify-content: center; margin: 10px 0; flex-wrap: wrap; }
.links a { color: #00ff88; text-decoration: none; border: 1px solid #00ff88; padding: 5px 10px; border-radius: 4px; font-size: 0.8em; }
.stats { display: flex; justify-content: space-around; margin-top: 15px; padding: 10px; background: #111; border-radius: 8px; flex-wrap: wrap; gap: 8px; }
.stat-val { font-size: 1.3em; font-weight: bold; text-align: center; }
.stat-lbl { color: #555; font-size: 0.65em; text-align: center; }
.session-bar { text-align: center; padding: 6px; border-radius: 4px; font-size: 0.85em; margin-bottom: 10px; }
.footer { text-align: center; color: #333; font-size: 0.7em; margin-top: 15px; padding-bottom: 20px; }
</style>
</head>
<body>
<h1>PHILIP'S TRADE DESK</h1>
<p class='sub'>""" + now.strftime("%Y-%m-%d %H:%M:%S") + """ EAT</p>
<div class='session-bar' style='background:#111;color:""" + session_color + """;border:1px solid """ + session_color + """;'>● """ + session_name + """</div>
<div class='links'>
<a href='/dashboard'>Refresh</a>
<a href='/weekly'>Weekly</a>
<a href='/scan'>JSON</a>
<a href='/health'>Health</a>
</div>
<h2>LIVE SIGNALS</h2>
<table>
<tr><th>Symbol</th><th>Score</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th><th>Size</th><th>Reason</th></tr>
""" + rows + """
</table>
<h2>SIGNAL STRENGTH</h2>
<div style='padding:10px;background:#0d0d15;border-radius:6px;'>""" + history_bars + """</div>
<h2>ACTIVE TRADES</h2>
<p class='sub'>🤖 = auto closed when SL/TP hit</p>
<table>
<tr><th>Symbol</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th><th>Score</th><th>Action</th></tr>
""" + active_rows + """
</table>
<h2>TODAY'S TRADE LOG</h2>
<div class='links'><a href='/clearlog' style='color:#888;border-color:#444;'>Clear Log</a></div>
<table>
<tr><th>Time</th><th>Date</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Pips</th><th>Result</th></tr>
""" + trade_log_rows + """
</table>
<div class='stats'>
<div><div class='stat-val' style='color:#00ff88'>""" + str(wins) + """</div><div class='stat-lbl'>WINS</div></div>
<div><div class='stat-val' style='color:#ff4444'>""" + str(losses) + """</div><div class='stat-lbl'>LOSSES</div></div>
<div><div class='stat-val' style='color:#ffaa00'>""" + str(winrate) + """%</div><div class='stat-lbl'>WIN RATE</div></div>
<div><div class='stat-val' style='color:""" + pips_color + """'>""" + str(total_pips) + """</div><div class='stat-lbl'>PIPS</div></div>
<div><div class='stat-val' style='color:""" + profit_color + """'>$""" + str(total_profit) + """</div><div class='stat-lbl'>P&L</div></div>
</div>
<h2>NEWS CALENDAR</h2>
<p class='sub' style='color:#ffaa00;'>Today highlighted · Signals blocked 30min before/after</p>
<table>
<tr><th>Day</th><th>Time</th><th>Event</th><th>Pairs</th></tr>
""" + news_rows + """
</table>
<p class='footer'>Philip's Trade Desk · Pinpoint System</p>
</body>
</html>"""
    return html


