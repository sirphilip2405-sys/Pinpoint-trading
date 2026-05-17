from flask import Flask, request, jsonify, redirect
import requests
from datetime import datetime, timezone, timedelta
import os

app = Flask(__name__)

WATCHLIST = ["BTCUSDT", "ETHUSDT", "GBPUSDT", "EURUSDT", "XAUUSDT", "USDTJPY"]
ACCOUNT_BALANCE = 10000
RISK_PCT = 0.01
MIN_LOT = 0.01
ATR_MULTIPLIER = 1.5
MIN_RR = 4.0
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

def get_eat_time():
    return datetime.now(timezone.utc) + timedelta(hours=3)

def is_kill_zone():
    now = get_eat_time()
    h = now.hour
    wd = now.weekday()
    if wd >= 5: return False
    return (10 <= h < 11) or (15 <= h < 17)

def is_good_trading_day():
    return get_eat_time().weekday() in [1, 2, 3]

def is_market_session():
    now = get_eat_time()
    h = now.hour
    wd = now.weekday()
    if wd >= 5: return False, 'Weekend - markets closed'
    if 10 <= h < 11: return True, 'London Open Kill Zone'
    if 15 <= h < 17: return True, 'NY Open Kill Zone'
    if 11 <= h < 15: return True, 'London Session'
    if 17 <= h < 20: return True, 'NY Session'
    return False, 'Off-session hours'

def check_news_filter(symbol):
    now = get_eat_time()
    wd = now.weekday()
    ct = now.hour * 60 + now.minute
    for e in NEWS_EVENTS:
        if symbol not in e['pairs']: continue
        if wd != e['day']: continue
        if abs(ct - (e['hour']*60 + e['minute'])) <= 30: return True, e['name']
    return False, None

def check_correlations(symbol, direction):
    return [s for s in CORRELATIONS.get(symbol, []) if s in active_trades and active_trades[s].get('direction') == direction]

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post("https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except: pass

def fetch_binance_ohlcv(symbol, interval, limit=100):
    try:
        url = "https://api.binance.com/api/v3/klines"
        r = requests.get(url, params={'symbol': symbol, 'interval': interval, 'limit': limit}, timeout=10)
        data = r.json()
        if isinstance(data, dict) and data.get('code'): return fetch_forex_ohlcv(symbol, interval, limit)
        return [{'open': float(d[1]), 'high': float(d[2]), 'low': float(d[3]), 'close': float(d[4]), 'volume': float(d[5])} for d in data]
    except: return fetch_forex_ohlcv(symbol, interval, limit)

def fetch_forex_ohlcv(symbol, interval, limit=100):
    try:
        fm = {'EURUSDT': 'EURUSD=X', 'GBPUSDT': 'GBPUSD=X', 'XAUUSDT': 'GC=F', 'USDTJPY': 'USDJPY=X'}
        tm = {'15m': '15m', '30m': '30m', '1h': '1h', '4h': '1h', '1d': '1d'}
        ticker = fm.get(symbol, symbol)
        iyf = tm.get(interval, '1h')
        period = '5d' if interval in ['15m','30m'] else '60d' if interval in ['4h','1d'] else '30d'
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + ticker
        r = requests.get(url, params={'interval': iyf, 'range': period}, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        data = r.json()
        result = data['chart']['result'][0]
        ts = result['timestamp']
        ohlcv = result['indicators']['quote'][0]
        candles = []
        for i in range(len(ts)):
            try: candles.append({'open': float(ohlcv['open'][i]), 'high': float(ohlcv['high'][i]), 'low': float(ohlcv['low'][i]), 'close': float(ohlcv['close'][i]), 'volume': float(ohlcv.get('volume', [1000]*len(ts))[i] or 1000)})
            except: continue
        return candles[-limit:] if len(candles) > limit else candles
    except: return None

def get_live_price(symbol):
    try:
        if symbol in ['EURUSDT','GBPUSDT','XAUUSDT','USDTJPY']:
            fm = {'EURUSDT': 'EURUSD=X', 'GBPUSDT': 'GBPUSD=X', 'XAUUSDT': 'GC=F', 'USDTJPY': 'USDJPY=X'}
            r = requests.get('https://query1.finance.yahoo.com/v8/finance/chart/' + fm[symbol], params={'interval': '1m', 'range': '1d'}, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            return float(r.json()['chart']['result'][0]['meta']['regularMarketPrice'])
        r = requests.get('https://api.binance.com/api/v3/ticker/price', params={'symbol': symbol}, timeout=10)
        return float(r.json()['price'])
    except: return None

def compute_atr(candles, period=14):
    trs = [max(c['high']-c['low'], abs(c['high']-candles[i-1]['close']), abs(c['low']-candles[i-1]['close'])) for i,c in enumerate(candles) if i > 0]
    return sum(trs[-period:]) / period if len(trs) >= period else (sum(trs)/len(trs) if trs else 0)

def compute_rsi(candles, period=14):
    closes = [c['close'] for c in candles]
    gains = [max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    losses = [max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    if len(gains) < period: return 50
    ag = sum(gains[-period:])/period
    al = sum(losses[-period:])/period
    return round(100-(100/(1+(ag/al if al else 999))),2)

def compute_bollinger(candles, period=20, std_dev=2.0):
    if len(candles) < period: return None, None, None
    closes = [c['close'] for c in candles[-period:]]
    mean = sum(closes)/period
    std = (sum((x-mean)**2 for x in closes)/period)**0.5
    return mean+std_dev*std, mean, mean-std_dev*std

def detect_structure(candles, lookback=10):
    if len(candles) < lookback*2+2: return {'trend':'neutral','bos':False,'choch':False,'swing_high':None,'swing_low':None}
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    rh = max(highs[-lookback:]); rl = min(lows[-lookback:])
    ph = max(highs[-lookback*2:-lookback]); pl = min(lows[-lookback*2:-lookback])
    close = candles[-1]['close']
    bos=False; choch=False; trend='neutral'
    if rh>ph and rl>pl: trend='bullish'; bos = close>rh
    elif rh<ph and rl<pl: trend='bearish'; bos = close<rl
    if ph<rh and close>rh: choch=True; trend='bullish'
    elif pl>rl and close<rl: choch=True; trend='bearish'
    return {'trend':trend,'bos':bos,'choch':choch,'swing_high':rh,'swing_low':rl}

def detect_fvg(candles):
    fvgs=[]
    for i in range(2,len(candles)):
        p=candles[i-2]; c=candles[i]
        if c['low']>p['high']: fvgs.append({'type':'bullish','top':c['low'],'bottom':p['high'],'mid':(c['low']+p['high'])/2})
        elif c['high']<p['low']: fvgs.append({'type':'bearish','top':p['low'],'bottom':c['high'],'mid':(p['low']+c['high'])/2})
    return fvgs[-5:] if fvgs else []

def detect_liquidity_sweep(candles, lookback=20):
    if len(candles)<lookback+3: return {'swept_high':False,'swept_low':False}
    recent=candles[-lookback-3:-3]; last3=candles[-3:]
    ph=max(c['high'] for c in recent); pl=min(c['low'] for c in recent)
    sh=any(c['high']>ph and c['close']<ph for c in last3)
    sl=any(c['low']<pl and c['close']>pl for c in last3)
    return {'swept_high':sh,'swept_low':sl,'prev_high':ph,'prev_low':pl}

def detect_market_mode(d1_candles, h4_candles):
    if not d1_candles or not h4_candles: return 'unknown'
    rsi=compute_rsi(d1_candles)
    upper,mean,lower=compute_bollinger(d1_candles)
    close=d1_candles[-1]['close']
    if upper and lower:
        if rsi>75 and close>upper: return 'reversal'
        if rsi<25 and close<lower: return 'reversal'
    h4s=detect_structure(h4_candles)
    if h4s['choch']: return 'transition'
    d1s=detect_structure(d1_candles)
    if d1s['trend']!='neutral': return 'trend'
    return 'range'

def calculate_pips(symbol, entry, exit_price, direction):
    m = 100 if symbol=='USDTJPY' else 10 if symbol=='XAUUSDT' else 1 if symbol in ['BTCUSDT','ETHUSDT'] else 10000
    return round((exit_price-entry if direction=='long' else entry-exit_price)*m, 1)

def compute_sovereign_score(symbol, tf_data, direction):
    score=0; reasons=[]
    d1=tf_data.get('D1',{}); h4=tf_data.get('H4',{}); h1=tf_data.get('H1',{}); m15=tf_data.get('M15',{})
    d1c=d1.get('candles',[]); h4c=h4.get('candles',[]); h1c=h1.get('candles',[]); m15c=m15.get('candles',[])
    if d1c:
        t=detect_structure(d1c).get('trend','neutral')
        if (direction=='long' and t=='bullish') or (direction=='short' and t=='bearish'): score+=10; reasons.append('D1 bias')
    if h4c:
        t=detect_structure(h4c).get('trend','neutral')
        if (direction=='long' and t=='bullish') or (direction=='short' and t=='bearish'): score+=10; reasons.append('H4 aligned')
    if h1c:
        t=detect_structure(h1c).get('trend','neutral')
        if (direction=='long' and t=='bullish') or (direction=='short' and t=='bearish'): score+=8; reasons.append('H1 aligned')
    if m15c:
        sw=detect_liquidity_sweep(m15c)
        if direction=='long' and sw.get('swept_low'): score+=15; reasons.append('Liq swept low')
        elif direction=='short' and sw.get('swept_high'): score+=15; reasons.append('Liq swept high')
    if h1c:
        fvgs=detect_fvg(h1c); close=h1c[-1]['close'] if h1c else 0
        for fvg in fvgs:
            if direction=='long' and fvg['type']=='bullish' and fvg['bottom']<=close<=fvg['top']: score+=10; reasons.append('H1 FVG'); break
            elif direction=='short' and fvg['type']=='bearish' and fvg['bottom']<=close<=fvg['top']: score+=10; reasons.append('H1 FVG'); break
    if m15c:
        s=detect_structure(m15c)
        if s.get('choch'): score+=8; reasons.append('M15 CHoCH OB')
    if is_kill_zone(): score+=8; reasons.append('Kill zone')
    nb,nn=check_news_filter(symbol)
    if not nb: score+=5; reasons.append('No news')
    else: score-=15; reasons.append('NEWS:'+nn)
    if m15c:
        rsi=compute_rsi(m15c)
        if 35<=rsi<=65: score+=5; reasons.append('RSI neutral')
    corr=check_correlations(symbol,direction)
    if not corr: score+=6; reasons.append('No corr conflict')
    else: reasons.append('Corr:'+str(corr))
    if is_good_trading_day(): score+=5; reasons.append('Good day')
    return min(max(score,0),100), reasons

def check_sl_tp_hits():
    for symbol in list(active_trades.keys()):
        try:
            t=active_trades[symbol]; price=get_live_price(symbol)
            if not price: continue
            entry=t.get('entry',0); sl=t.get('stop_loss',0); tp=t.get('take_profit',0); d=t.get('direction','')
            hit=None
            if d=='long':
                if price>=tp: hit='win'
                elif price<=sl: hit='loss'
            elif d=='short':
                if price<=tp: hit='win'
                elif price>=sl: hit='loss'
            if hit:
                now=get_eat_time(); ep=tp if hit=='win' else sl
                pips=calculate_pips(symbol,entry,ep,d)
                rd=abs(entry-sl)
                pusd=round(pips*(ACCOUNT_BALANCE*RISK_PCT/rd) if rd>0 else 0,2)
                trade_log.append({'time':now.strftime('%H:%M'),'date':now.strftime('%Y-%m-%d'),'symbol':symbol,'direction':d,'entry':entry,'exit':round(ep,5),'stop_loss':sl,'take_profit':tp,'score':t.get('score',0),'sovereign_score':t.get('sovereign_score',0),'result':hit,'pips':pips,'profit_usd':pusd,'auto':True})
                del active_trades[symbol]
                emoji='✅' if hit=='win' else '❌'
                send_telegram(emoji+' <b>AUTO CLOSED</b>\n<b>'+symbol+'</b> '+d.upper()+'\nResult: <b>'+hit.upper()+'</b>\nPips: '+str(pips)+' | P&L: $'+str(pusd))
        except: continue

def analyze(symbol):
    tf_map={'M15':'15m','M30':'30m','H1':'1h','H4':'4h','D1':'1d'}
    tf_data={}
    for tf,interval in tf_map.items():
        candles=fetch_binance_ohlcv(symbol,interval)
        if candles: tf_data[tf]={'candles':candles,'atr':compute_atr(candles),'rsi':compute_rsi(candles),'structure':detect_structure(candles)}
    if not tf_data: return {'symbol':symbol,'score':0,'reason':'No data','signal':None}
    h1=tf_data.get('H1',{}); m15=tf_data.get('M15',{}); m30=tf_data.get('M30',{}); h4=tf_data.get('H4',{}); d1=tf_data.get('D1',{})
    h1t=h1.get('structure',{}).get('trend','neutral') if h1 else 'neutral'
    m15t=m15.get('structure',{}).get('trend','neutral') if m15 else 'neutral'
    m30t=m30.get('structure',{}).get('trend','neutral') if m30 else 'neutral'
    mode='unknown'
    if d1 and h4: mode=detect_market_mode(d1.get('candles',[]),h4.get('candles',[]))
    if h1t!=m15t or h1t=='neutral': return {'symbol':symbol,'score':0,'reason':'Mismatch H1='+h1t+' M15='+m15t,'signal':None}
    direction='long' if h1t=='bullish' else 'short'
    score=30; reasons=['H1+M15 confluent ('+h1t+')']
    m15s=m15.get('structure',{}) if m15 else {}
    if m15s.get('choch'): score+=25; reasons.append('M15 CHoCH')
    elif m15s.get('bos'): score+=20; reasons.append('M15 BoS')
    h1s=h1.get('structure',{}) if h1 else {}
    if h1s.get('bos') or h1s.get('choch'): score+=15; reasons.append('H1 structure')
    m15r=m15.get('rsi',50) if m15 else 50
    if direction=='long' and 30<m15r<65: score+=10; reasons.append('RSI ok')
    elif direction=='short' and 35<m15r<70: score+=10; reasons.append('RSI ok')
    if m30t==h1t: score+=10; reasons.append('M30 confirms')
    sov,sov_reasons=compute_sovereign_score(symbol,tf_data,direction)
    combined=round(score*0.35+sov*0.65,1)
    all_reasons=reasons+[r for r in sov_reasons if r not in reasons]
    entry=m15['candles'][-1]['close'] if m15 else 0
    atr=m15.get('atr',entry*0.001) if m15 else entry*0.001
    sh=m15s.get('swing_high'); sl=m15s.get('swing_low')
    if direction=='long':
        sl_atr=entry-ATR_MULTIPLIER*atr
        stop_loss=max(sl_atr, sl if sl and sl<entry else sl_atr)
    else:
        sl_atr=entry+ATR_MULTIPLIER*atr
        stop_loss=min(sl_atr, sh if sh and sh>entry else sl_atr)
    risk=abs(entry-stop_loss)
    if risk<0.000001: return {'symbol':symbol,'score':combined,'reason':'Invalid SL','signal':None}
    rr=6.0 if mode=='trend' else 4.0 if mode=='reversal' else MIN_RR
    tp=entry+risk*rr if direction=='long' else entry-risk*rr
    tp1=entry+risk*2 if direction=='long' else entry-risk*2
    size=round((ACCOUNT_BALANCE*RISK_PCT)/risk,4) if risk>0 else MIN_LOT
    corr=check_correlations(symbol,direction)
    signal={'symbol':symbol,'direction':direction,'entry':round(entry,5),'stop_loss':round(stop_loss,5),'take_profit':round(tp,5),'tp1':round(tp1,5),'rr_ratio':rr,'position_size':size,'risk_usd':round(ACCOUNT_BALANCE*RISK_PCT,2),'score':combined,'sovereign_score':sov,'market_mode':mode,'reason':' | '.join(all_reasons),'correlation_warning':corr}
    live_signals[symbol]=signal
    if symbol not in active_trades: active_trades[symbol]=signal
    if symbol not in signal_history: signal_history[symbol]=[]
    signal_history[symbol].append({'time':get_eat_time().strftime('%H:%M'),'score':combined,'sovereign_score':sov,'direction':direction,'mode':mode})
    signal_history[symbol]=signal_history[symbol][-20:]
    sk=symbol+'_'+str(combined)+'_'+direction
    if combined>=70 and sk not in notified_signals:
        notified_signals.add(sk)
        me='📈' if mode=='trend' else '🔄' if mode=='reversal' else '⚡'
        ct='\nCorr:'+str(corr) if corr else ''
        send_telegram('🎯 <b>SOVEREIGN SIGNAL</b> '+me+'\n\n<b>'+symbol+'</b> — '+direction.upper()+'\nMode: '+mode.upper()+'\nScore: '+str(combined)+'/100\nSovereign: '+str(sov)+'/100\n\nEntry: '+str(round(entry,5))+'\nSL: '+str(round(stop_loss,5))+'\nTP1: '+str(round(tp1,5))+' (1:2)\nTP2: '+str(round(tp,5))+' (1:'+str(rr)+')\nSize: '+str(size)+ct)
    return {'symbol':symbol,'score':combined,'sovereign_score':sov,'market_mode':mode,'direction':direction,'reason':' | '.join(all_reasons),'correlation_warning':corr,'signal':signal}

@app.route('/health')
def health():
    sa,sn=is_market_session()
    return jsonify({'status':'running','time_eat':get_eat_time().strftime('%Y-%m-%d %H:%M:%S'),'session':sn,'session_active':sa,'kill_zone':is_kill_zone(),'good_trading_day':is_good_trading_day(),'watchlist':WATCHLIST,'active_trades':len(active_trades),'telegram_configured':bool(TELEGRAM_TOKEN)})

@app.route('/scan')
def scan():
    results=[]
    for symbol in WATCHLIST:
        try: results.append(analyze(symbol))
        except Exception as e: results.append({'symbol':symbol,'score':0,'reason':str(e)})
    results.sort(key=lambda x:x['score'],reverse=True)
    return jsonify(results)

@app.route('/analyze/<symbol>')
def analyze_symbol(symbol): return jsonify(analyze(symbol.upper()))

@app.route('/signals')
def signals(): return jsonify(live_signals)

@app.route('/webhook', methods=['POST'])
def webhook():
    data=request.json; symbol=data.get('symbol','').upper(); tf=data.get('timeframe','').upper()
    if symbol and tf:
        if symbol not in webhook_cache: webhook_cache[symbol]={}
        webhook_cache[symbol][tf]=data
    return jsonify({'status':'ok'})

@app.route('/close_trade')
def close_trade():
    symbol=request.args.get('symbol','').upper(); result=request.args.get('result','')
    if symbol in active_trades and result in ['win','loss']:
        t=active_trades[symbol]; now=get_eat_time()
        entry=t.get('entry',0); sl=t.get('stop_loss',0); d=t.get('direction','')
        ep=t.get('take_profit',0) if result=='win' else sl
        pips=calculate_pips(symbol,entry,ep,d)
        rd=abs(entry-sl)
        pusd=round(pips*(ACCOUNT_BALANCE*RISK_PCT/rd) if rd>0 else 0,2)
        trade_log.append({'time':now.strftime('%H:%M'),'date':now.strftime('%Y-%m-%d'),'symbol':symbol,'direction':d,'entry':entry,'exit':round(ep,5),'stop_loss':sl,'take_profit':t.get('take_profit',0),'score':t.get('score',0),'sovereign_score':t.get('sovereign_score',0),'result':result,'pips':pips,'profit_usd':pusd,'auto':False})
        del active_trades[symbol]
        emoji='✅' if result=='win' else '❌'
        send_telegram(emoji+' <b>TRADE CLOSED</b>\n<b>'+symbol+'</b> '+d.upper()+'\nResult: <b>'+result.upper()+'</b>\nPips: '+str(pips)+' | P&L: $'+str(pusd))
    return redirect('/dashboard')

@app.route('/cancel_trade')
def cancel_trade():
    symbol=request.args.get('symbol','').upper()
    if symbol in active_trades: del active_trades[symbol]
    return redirect('/dashboard')

@app.route('/clearlog')
def clear_log():
    trade_log.clear(); notified_signals.clear()
    return redirect('/dashboard')

@app.route('/weekly')
def weekly():
    now=get_eat_time(); by_pair={}; by_day={}
    for t in trade_log:
        sym=t['symbol']
        if sym not in by_pair: by_pair[sym]={'wins':0,'losses':0,'pips':0}
        by_pair[sym]['wins' if t['result']=='win' else 'losses']+=1
        by_pair[sym]['pips']+=t.get('pips',0)
        day=t.get('date','unknown')
        if day not in by_day: by_day[day]={'wins':0,'losses':0}
        by_day[day]['wins' if t['result']=='win' else 'losses']+=1
    tw=len([t for t in trade_log if t['result']=='win'])
    tl=len([t for t in trade_log if t['result']=='loss'])
    tot=tw+tl; wr=round(tw/tot*100) if tot>0 else 0
    tp=round(sum(t.get('pips',0) for t in trade_log),1)
    tpusd=round(sum(t.get('profit_usd',0) for t in trade_log),2)
    pr=''
    for sym,d in by_pair.items():
        t=d['wins']+d['losses']; wr2=round(d['wins']/t*100) if t>0 else 0
        pc='#00ff88' if d['pips']>=0 else '#ff4444'
        pr+='<tr><td style=color:#fff>'+sym+'</td><td style=color:#00ff88>'+str(d['wins'])+'</td><td style=color:#ff4444>'+str(d['losses'])+'</td><td style=color:#ffaa00>'+str(wr2)+'%</td><td style=color:'+pc+'>'+str(round(d['pips'],1))+'</td></tr>'
    if not pr: pr='<tr><td colspan=5 style=color:#444;text-align:center;padding:15px>No data yet</td></tr>'
    dr=''
    for day,d in sorted(by_day.items(),reverse=True):
        t=d['wins']+d['losses']; wr2=round(d['wins']/t*100) if t>0 else 0
        dr+='<tr><td style=color:#888>'+day+'</td><td style=color:#00ff88>'+str(d['wins'])+'</td><td style=color:#ff4444>'+str(d['losses'])+'</td><td style=color:#ffaa00>'+str(wr2)+'%</td></tr>'
    if not dr: dr='<tr><td colspan=4 style=color:#444;text-align:center;padding:15px>No data yet</td></tr>'
    h+='<h1>WEEKLY REPORT</h1>'
    h+='<p style=text-align:center;color:#555;font-size:0.8em;margin-bottom:10px>'+now.strftime('%Y-%m-%d %H:%M')+' EAT</p>'
    h+='<div class=links><a href=/dashboard>Dashboard</a></div>'
    h+='<div class=stats>'
    h+='<div><div class=sv style=color:#00ff88>'+str(tw)+'</div><div class=sl>WINS</div></div>'
    h+='<div><div class=sv style=color:#ff4444>'+str(tl)+'</div><div class=sl>LOSSES</div></div>'
    h+='<div><div class=sv style=color:#ffaa00>'+str(wr)+'%</div><div class=sl>WIN RATE</div></div>'
    h+='<div><div class=sv style=color:#00ff88>'+str(tp)+'</div><div class=sl>PIPS</div></div>'
    h+='<div><div class=sv style=color:#fff>$'+str(tpusd)+'</div><div class=sl>P&L</div></div>'
    h+='</div>'
    h+='<h2>BY PAIR</h2><table><tr><th>Pair</th><th>W</th><th>L</th><th>WR%</th><th>Pips</th></tr>'+pr+'</table>'
    h+='<h2>BY DAY</h2><table><tr><th>Date</th><th>W</th><th>L</th><th>WR%</th></tr>'+dr+'</table>'
    h+='</body></html>'
    return h

@app.route('/dashboard')
def dashboard():
    try: check_sl_tp_hits()
    except: pass
    results=[]
    for symbol in WATCHLIST:
        try: results.append(analyze(symbol))
        except Exception as e: results.append({'symbol':symbol,'score':0,'reason':str(e),'signal':None})
    results.sort(key=lambda x:x['score'],reverse=True)
    sa,sn=is_market_session(); sc='#00ff88' if sa else '#ff4444'
    kz=is_kill_zone(); gd=is_good_trading_day()
    kc='#00ff88' if kz else '#555'; dc='#00ff88' if gd else '#ff4444'
    rows=''
    for r in results:
        score=r.get('score',0); sov=r.get('sovereign_score',0)
        sym=r.get('symbol',''); reason=r.get('reason',''); direction=r.get('direction','-'); mode=r.get('market_mode','unknown')
        sig=r.get('signal') or {}; entry=sig.get('entry','-'); sl=sig.get('stop_loss','-'); tp1=sig.get('tp1','-'); tp2=sig.get('take_profit','-'); rr=sig.get('rr_ratio',MIN_RR); size=sig.get('position_size','-')
        corr=r.get('correlation_warning',[])
        color='#00ff88' if score>=70 else '#ffaa00' if score>=50 else '#ff4444'
        emoji='🟢' if score>=70 else '🟡' if score>=50 else '🔴'
        me='📈' if mode=='trend' else '🔄' if mode=='reversal' else '⚡' if mode=='transition' else '〰'
        dc2='#00ff88' if direction=='long' else '#ff4444' if direction=='short' else '#888'
        rs=reason[:35]+'...' if len(reason)>35 else reason
        cf=' ⚠️' if corr else ''
        rows+='<tr><td><b style=color:#fff>'+sym+'</b></td><td><span style=color:'+color+';font-weight:bold>'+emoji+' '+str(score)+'</span></td><td style=color:#888;font-size:0.8em>'+str(sov)+'</td><td>'+me+'<span style=color:#666;font-size:0.7em>'+mode[:4]+'</span></td><td><span style=color:'+dc2+'>'+((direction.upper()) if direction and direction!='-' else '-')+'</span></td><td style=color:#ccc>'+str(entry)+'</td><td style=color:#ff6b6b>'+str(sl)+'</td><td style=color:#88ff88;font-size:0.8em>'+str(tp1)+'</td><td style=color:#00ff88>'+str(tp2)+'</td><td style=color:#ffaa00>1:'+str(rr)+'</td><td style=color:#aaa>'+str(size)+'</td><td style=color:#555;font-size:0.7em>'+rs+cf+'</td></tr>'
    ar=''
    if active_trades:
        for sym,t in active_trades.items():
            dc2='#00ff88' if t.get('direction')=='long' else '#ff4444'
            cb=' ⚠️' if t.get('correlation_warning',[]) else ''
            ar+='<tr><td style=color:#fff>'+sym+cb+'</td><td style=color:'+dc2+'>'+((t.get('direction') or '')).upper()+'</td><td style=color:#ccc>'+str(t.get('entry','-'))+'</td><td style=color:#ff6b6b>'+str(t.get('stop_loss','-'))+'</td><td style=color:#88ff88;font-size:0.8em>'+str(t.get('tp1','-'))+'</td><td style=color:#00ff88>'+str(t.get('take_profit','-'))+'</td><td style=color:#ffaa00>'+str(t.get('score','-'))+'</td><td style=color:#888>'+str(t.get('sovereign_score','-'))+'</td><td><a href=/close_trade?symbol='+sym+'&result=win style=color:#00ff88;text-decoration:none;border:1px solid #00ff88;padding:2px 5px;border-radius:3px;font-size:0.75em;margin-right:3px>WIN</a><a href=/close_trade?symbol='+sym+'&result=loss style=color:#ff4444;text-decoration:none;border:1px solid #ff4444;padding:2px 5px;border-radius:3px;font-size:0.75em;margin-right:3px>LOSS</a><a href=/cancel_trade?symbol='+sym+' style=color:#888;text-decoration:none;border:1px solid #444;padding:2px 5px;border-radius:3px;font-size:0.75em>X</a></td></tr>'
    else: ar='<tr><td colspan=9 style=color:#444;text-align:center;padding:15px>No active trades</td></tr>'
    wins=len([t for t in trade_log if t['result']=='win'])
    losses=len([t for t in trade_log if t['result']=='loss'])
    total=wins+losses; wr=round(wins/total*100) if total>0 else 0
    tpips=round(sum(t.get('pips',0) for t in trade_log),1)
    tprofit=round(sum(t.get('profit_usd',0) for t in trade_log),2)
    pic='#00ff88' if tpips>=0 else '#ff4444'; prc='#00ff88' if tprofit>=0 else '#ff4444'
    tlr=''
    for t in reversed(trade_log):
        rc='#00ff88' if t['result']=='win' else '#ff4444'
        dc2='#00ff88' if t['direction']=='long' else '#ff4444'
        pc='#00ff88' if t.get('pips',0)>=0 else '#ff4444'
        ab=' 🤖' if t.get('auto') else ''
        tlr+='<tr><td style=color:#888>'+t['time']+'</td><td style=color:#777>'+t['date']+'</td><td style=color:#fff>'+t['symbol']+'</td><td style=color:'+dc2+'>'+t['direction'].upper()+'</td><td style=color:#ccc>'+str(t['entry'])+'</td><td style=color:#aaa>'+str(t.get('exit','-'))+'</td><td style=color:'+pc+'>'+str(t.get('pips','-'))+'</td><td style=color:#777>$'+str(t.get('profit_usd','-'))+'</td><td style=color:'+rc+';font-weight:bold>'+('WIN' if t['result']=='win' else 'LOSS')+ab+'</td></tr>'
    if not trade_log: tlr='<tr><td colspan=9 style=color:#444;text-align:center;padding:20px>No trades yet</td></tr>'
    hb=''
    for sym in WATCHLIST:
        hist=signal_history.get(sym,[])
        if hist:
            last=hist[-1]; sv=last['score']; sovv=last.get('sovereign_score',0)
            bc='#00ff88' if sv>=70 else '#ffaa00' if sv>=50 else '#ff4444'
            hb+='<div style=margin-bottom:10px><div style=display:flex;justify-content:space-between;color:#888;font-size:0.75em;margin-bottom:2px><span style=color:#fff>'+sym+'</span><span style=color:#666>'+last.get('mode','')+'</span><span style=color:'+bc+'>'+str(sv)+' SOV:'+str(sovv)+'</span></div><div style=background:#111;border-radius:3px;height:6px;margin-bottom:2px><div style=background:'+bc+';width:'+str(int(sv))+'%;height:6px;border-radius:3px></div></div><div style=background:#111;border-radius:3px;height:4px><div style=background:#4444ff;width:'+str(int(sovv))+'%;height:4px;border-radius:3px></div></div></div>'
    if not hb: hb='<p style=color:#444;text-align:center;padding:10px;font-size:0.8em>No history yet</p>'
    nr=''; now_eat=get_eat_time(); days=['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
    for event in NEWS_EVENTS:
        dn=days[event['day']]; ts=str(event['hour']).zfill(2)+':'+str(event['minute']).zfill(2)
        ps=', '.join([p.replace('USDT','') for p in event['pairs']])
        it=now_eat.weekday()==event['day']; rc='#ffaa00' if it else '#555'
        nr+='<tr><td style=color:'+rc+'>'+dn+'</td><td style=color:'+rc+'>'+ts+' EAT</td><td style=color:#fff>'+event['name']+'</td><td style=color:#888;font-size:0.8em>'+ps+'</td></tr>'
    now=get_eat_time()
    h+="<h1>PHILIPS TRADE DESK</h1>"
    h+='<div style=text-align:center;margin-bottom:6px><span class=badge style=background:#0d1a0d;color:'+sc+';border:1px solid '+sc+'>● '+sn+'</span><span class=badge style=background:#0d0d1a;color:'+kc+';border:1px solid '+kc+'>⚡ '+('KILL ZONE' if kz else 'no kill zone')+'</span><span class=badge style=background:#1a0d0d;color:'+dc+';border:1px solid '+dc+'>📅 '+('GOOD DAY' if gd else 'low prob day')+'</span></div>'
    h+='<p class=sub>'+now.strftime('%Y-%m-%d %H:%M:%S')+' EAT · Auto-refresh 30s</p>'
    h+='<div class=links><a href=/dashboard>Refresh</a><a href=/weekly>Weekly</a><a href=/scan>JSON</a><a href=/health>Health</a></div>'
    h+='<h2>LIVE SIGNALS</h2><p class=sub>Min score 70 · Green=Score Blue=Sovereign</p>'
    h+='<table><tr><th>Pair</th><th>Score</th><th>SOV</th><th>Mode</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>RR</th><th>Lot</th><th>Reason</th></tr>'+rows+'</table>'
    h+='<h2>SIGNAL STRENGTH</h2><div style=padding:10px;background:#0d0d15;border-radius:6px>'+hb+'</div>'
    h+='<h2>ACTIVE TRADES</h2><p class=sub>🤖=auto · TP1 close 50% · TP2 close rest</p>'
    h+='<table><tr><th>Pair</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>Score</th><th>SOV</th><th>Action</th></tr>'+ar+'</table>'
    h+='<h2>TRADE LOG</h2><div class=links><a href=/clearlog style=color:#888;border-color:#444>Clear</a></div>'
    h+='<table><tr><th>Time</th><th>Date</th><th>Pair</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Pips</th><th>P&L</th><th>Result</th></tr>'+tlr+'</table>'
    h+='<div class=stats><div><div class=sv style=color:#00ff88>'+str(wins)+'</div><div class=sl>WINS</div></div><div><div class=sv style=color:#ff4444>'+str(losses)+'</div><div class=sl>LOSSES</div></div><div><div class=sv style=color:#ffaa00>'+str(wr)+'%</div><div class=sl>WIN RATE</div></div><div><div class=sv style=color:'+pic+'>'+str(tpips)+'</div><div class=sl>PIPS</div></div><div><div class=sv style=color:'+prc+'>$'+str(tprofit)+'</div><div class=sl>P&L</div></div></div>'
    h+='<h2>NEWS CALENDAR</h2><p class=sub style=color:#ffaa00>Today highlighted · Blocked 30min before/after</p>'
    h+='<table><tr><th>Day</th><th>Time</th><th>Event</th><th>Pairs</th></tr>'+nr+'</table>'
    h+='<p class=footer>Philip Trade Desk · SOVEREIGN + APEX Engine</p>'
    h+='</body></html>'
    return h

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False)
