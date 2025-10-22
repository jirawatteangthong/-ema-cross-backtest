#!/usr/bin/env python3
"""
OKX EMA Sideway Scalper — Multi-Symbol Safe Mode (Rank A) — FINAL

- Scan 3 pairs: XRP, DOGE, TRX (fixed in code)
- Choose the safest signal (Rank A = lowest ATR) and open ONLY ONE position at a time
- Sideway filter (EMA9/21 gap + ATR% + EMA50 slope)
- Mean reversion entries around EMA9
- TP/SL in ATR units
- Single DCA (fixed size), Basket TP
- Limit max 20 trades/day
- Stop trading for the day after 5 consecutive SLs (notify Telegram)
- Daily summary to Telegram once/day (23:55 Asia/Bangkok)
- Timeframe 5m
- Leverage 10x (Isolated + Net mode on OKX)
- ENV needed only for OKX creds + Telegram (5 keys)

REQUIRED ENV:
  OKX_API_KEY, OKX_SECRET, OKX_PASSWORD
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

import os, time, math, traceback
from datetime import datetime
import pytz, requests
import ccxt
import numpy as np

# ======== ENV (ONLY exchange + telegram) =========
API_KEY = os.getenv('OKX_API_KEY', '')
SECRET = os.getenv('OKX_SECRET', '')
PASSWORD = os.getenv('OKX_PASSWORD', '')

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# ======== FIXED STRATEGY PARAMS (EDIT HERE IF NEEDED) =========
SYMBOLS = ["XRP/USDT:USDT", "DOGE/USDT:USDT", "TRX/USDT:USDT"]
TIMEFRAME = "5m"
LEVERAGE = 10  # คุณตั้งให้ 10x ก่อน ถ้าดีค่อยปรับ
POLL_SECONDS = 5

# Position sizing: เลือกได้ 2 โหมด
FIXED_NOTIONAL_USDT = 0.0   # ถ้า > 0 จะใช้จำนวน USDT ต่อไม้เป็นค่าคงที่ (เช่น 1.5)
RISK_PER_TRADE = 0.01       # ถ้า FIXED_NOTIONAL_USDT == 0.0 จะใช้ % ของ Equity (เช่น 0.01 = 1%)

MAX_TRADES_PER_DAY = 20
STOP_AFTER_SL_STREAK = 5    # SL ติดกัน 5 ครั้ง -> หยุดทั้งวัน

DAILY_SUMMARY_HOUR = 23
DAILY_SUMMARY_MINUTE = 55

# Indicators
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 50
ATR_PERIOD = 14

# Filters (Safe Mode)
EMA_GAP_MAX = 0.001     # 0.10%
ATR_PCT_MIN = 0.002     # 0.20%
ATR_PCT_MAX = 0.008     # 0.80%
EMA50_SLOPE_MAX = 0.0003  # ~0.03%/bar

# Entry/Exit in ATR units
EXTENSION_ATR = 0.35
TP_ATR = 0.25
SL_ATR = 0.55
DCA_TRIGGER_ATR = 0.30
BASKET_TP_ATR = 0.10

BANGKOK = pytz.timezone('Asia/Bangkok')

STATE = {
    "today": None,
    "trades_today": 0,
    "loss_streak": 0,
    "halt_for_today": False,
    "summary": {"wins":0, "losses":0, "closed_pnl_usdt":0.0, "trades":0},
    "sent_summary_for": None,
    # active position
    "active_symbol": None,
    "entry_side": None,         # "long"/"short"
    "entry_price_1": None,
    "entry_amount": 0.0,
    "safety_used": False,
    "sl_price": None,
    "basket_tp_usdt": None,
}

# ======== Utils ========
def ema(arr, period):
    if len(arr) < period: return np.array([np.nan]*len(arr))
    k = 2/(period+1)
    out = np.empty_like(arr, dtype=float); out[:] = np.nan
    out[period-1] = np.mean(arr[:period])
    for i in range(period, len(arr)):
        out[i] = arr[i]*k + out[i-1]*(1-k)
    return out

def true_range(h, l, c):
    tr = [np.nan]
    for i in range(1, len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return np.array(tr, dtype=float)

def atr(h, l, c, period=14):
    tr = true_range(h, l, c)
    out = np.array([np.nan]*len(c), dtype=float)
    if len(c) < period+1: return out
    out[period] = np.nanmean(tr[1:period+1])
    for i in range(period+1, len(c)):
        out[i] = (out[i-1]*(period-1) + tr[i]) / period
    return out

def telegram_send(text):
    if not TELEGRAM_TOKEN:
        print("[TELEGRAM] (skip) ", text); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode":"HTML"},
            timeout=10
        )
    except Exception as e:
        print("Telegram error:", e)

def now_bkk(): return datetime.now(BANGKOK)
def same_day(a,b): return a.date()==b.date()

def ensure_new_day():
    n = now_bkk()
    if STATE["today"] is None or not same_day(n, STATE["today"]):
        STATE["today"] = n
        STATE["trades_today"] = 0
        STATE["loss_streak"] = 0
        STATE["halt_for_today"] = False
        STATE["summary"] = {"wins":0,"losses":0,"closed_pnl_usdt":0.0,"trades":0}
        STATE["sent_summary_for"] = None

def maybe_send_daily_summary(force=False):
    n = now_bkk(); ensure_new_day()
    key = n.strftime("%Y-%m-%d")
    should_time = (n.hour == DAILY_SUMMARY_HOUR and n.minute >= DAILY_SUMMARY_MINUTE)
    if force or (should_time and STATE["sent_summary_for"] != key):
        s = STATE["summary"]
        msg = (
            f"📊 <b>Daily Summary</b> ({key})\n"
            f"Trades: {s['trades']}\n"
            f"Wins: {s['wins']}  Losses: {s['losses']}\n"
            f"PNL: {s['closed_pnl_usdt']:.2f} USDT\n"
        )
        telegram_send(msg)
        STATE["sent_summary_for"] = key

# ======== Exchange helpers ========
def create_exchange():
    exchange = ccxt.okx({
        "apiKey": API_KEY,
        "secret": SECRET,
        "password": PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "positionSide": "net",
        }
    })
    exchange.load_markets()
    for sym in SYMBOLS:
        try:
            exchange.set_margin_mode("isolated", sym, params={"posSide":"net"})
        except Exception as e:
            print(f"Set margin mode failed ({sym}):", e)
        try:
            exchange.set_leverage(LEVERAGE, sym, params={"mgnMode":"isolated","posSide":"net"})
        except Exception as e:
            print(f"Set leverage failed ({sym}):", e)
    return exchange

def fetch_ohlcv(ex, symbol, timeframe, limit=200):
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    o,h,l,c,t = [],[],[],[],[]
    for x in ohlcv:
        t.append(x[0]); o.append(x[1]); h.append(x[2]); l.append(x[3]); c.append(x[4])
    return {
        "ts": np.array(t, dtype=np.int64),
        "open": np.array(o, dtype=float),
        "high": np.array(h, dtype=float),
        "low": np.array(l, dtype=float),
        "close": np.array(c, dtype=float),
    }

def indicators(data):
    c,h,l = data["close"], data["high"], data["low"]
    e9 = ema(c, EMA_FAST); e21 = ema(c, EMA_SLOW); e50 = ema(c, EMA_TREND)
    a = atr(h, l, c, ATR_PERIOD)
    return e9, e21, e50, a

def sideway_filter(e9,e21,e50,a,c):
    i=-1
    if any(len(arr)<2 or np.isnan(arr[i]) for arr in [e9,e21,e50,a]): return False
    price = c[i]
    gap   = abs(e9[i]-e21[i])/price
    atrp  = a[i]/price
    slope = abs(e50[i]-e50[i-1])/price
    return (gap<=EMA_GAP_MAX) and (ATR_PCT_MIN<=atrp<=ATR_PCT_MAX) and (slope<=EMA50_SLOPE_MAX)

def generate_signal(e9,a,c):
    i=-1
    if np.isnan(e9[i]) or np.isnan(a[i]): return None
    price = c[i]; prev_price = c[i-1]; prev_e9 = e9[i-1]
    if prev_price < (prev_e9 - EXTENSION_ATR*a[i-1]) and price > e9[i]:
        return "long"
    if prev_price > (prev_e9 + EXTENSION_ATR*a[i-1]) and price < e9[i]:
        return "short"
    return None

def get_equity_usdt(ex):
    bal = ex.fetch_balance(params={"type":"swap"})
    usdt = bal.get("USDT", {})
    eq = usdt.get("total")
    if eq is None:
        try: eq = float(usdt.get("info", {}).get("eq", 0))
        except: eq = 0
    return float(eq or 0.0)

def amount_to_precision(ex, symbol, amount):
    return float(ex.amount_to_precision(symbol, amount))

def enforce_min_amount(ex, symbol, amount):
    m = ex.market(symbol)
    try:
        min_amt = m.get('limits', {}).get('amount', {}).get('min', None)
        if min_amt is None: return amount
        return max(float(min_amt), float(amount))
    except:
        return amount

def ticker(ex, symbol):
    t = ex.fetch_ticker(symbol)
    return float(t['last'])

def fetch_positions_map(ex, symbols):
    pos_list = ex.fetch_positions(symbols)
    mp = {s: None for s in symbols}
    for p in pos_list:
        sym = p.get('symbol')
        if sym in mp:
            amt = float(p.get('contracts') or p.get('size') or 0)
            if amt != 0: mp[sym] = p
    return mp

def unrealized_pnl_usdt(pos):
    if not pos: return 0.0
    try: return float(pos.get('unrealizedPnl', 0.0))
    except: return 0.0

def close_all_positions(ex, symbol):
    positions = ex.fetch_positions([symbol])
    for p in positions:
        if p.get('symbol') != symbol: continue
        amt = float(p.get('contracts') or p.get('size') or 0)
        side = (p.get('side') or '').lower()
        if amt and side:
            try:
                if side == 'long': ex.create_order(symbol, 'market', 'sell', amt)
                else: ex.create_order(symbol, 'market', 'buy', amt)
            except Exception as e:
                print("close_all error:", e)

# ======== Ranking ========
def pick_best_candidate(candidates):
    # Rank A: lowest ATR (safest)
    if not candidates: return None
    return min(candidates, key=lambda x: x["atr"])

# ======== Main Loop ========
def run():
    ex = create_exchange()
    print("Markets loaded. Multi-symbol on", SYMBOLS, TIMEFRAME)
    ensure_new_day()

    while True:
        try:
            ensure_new_day()
            maybe_send_daily_summary(False)

            # Halt condition (after 5 SLs)
            if STATE["halt_for_today"]:
                time.sleep(POLL_SECONDS); continue

            # Active position?
            pos_map = fetch_positions_map(ex, SYMBOLS)
            active = None
            for sym, p in pos_map.items():
                if p is not None:
                    active = sym; break

            # If have active position -> manage it
            if active or STATE["active_symbol"]:
                symbol = active or STATE["active_symbol"]
                if STATE["active_symbol"] is None:
                    STATE["active_symbol"] = symbol

                price = ticker(ex, symbol)

                # DCA one time
                if (not STATE["safety_used"]) and (STATE["entry_price_1"] is not None):
                    data = fetch_ohlcv(ex, symbol, TIMEFRAME, limit=EMA_TREND+ATR_PERIOD+5)
                    e9,e21,e50,a = indicators(data)
                    if len(a)>0 and not math.isnan(a[-1]):
                        adverse = (price <= STATE["entry_price_1"] - DCA_TRIGGER_ATR*a[-1]) if STATE["entry_side"]=='long' else (price >= STATE["entry_price_1"] + DCA_TRIGGER_ATR*a[-1])
                        if adverse:
                            side2 = 'buy' if STATE["entry_side"]=='long' else 'sell'
                            try:
                                ex.create_order(symbol, 'market', side2, STATE["entry_amount"])
                                STATE["safety_used"] = True
                            except Exception as e:
                                print("DCA error:", e)

                # Check Basket TP or SL
                pos_map = fetch_positions_map(ex, [symbol])
                p = pos_map.get(symbol)
                pnl = unrealized_pnl_usdt(p)
                hit_basket_tp = (STATE["basket_tp_usdt"] is not None) and (pnl >= STATE["basket_tp_usdt"])
                hit_sl = False
                if STATE["entry_side"]=='long' and STATE["sl_price"] is not None and price <= STATE["sl_price"]:
                    hit_sl = True
                if STATE["entry_side"]=='short' and STATE["sl_price"] is not None and price >= STATE["sl_price"]:
                    hit_sl = True

                if hit_basket_tp or hit_sl:
                    try:
                        close_all_positions(ex, symbol)
                        pnl_final = pnl
                        STATE["summary"]["trades"] += 1
                        STATE["summary"]["closed_pnl_usdt"] += pnl_final
                        if hit_basket_tp:
                            STATE["summary"]["wins"] += 1
                            STATE["loss_streak"] = 0
                        else:
                            STATE["summary"]["losses"] += 1
                            STATE["loss_streak"] += 1
                            # check halt
                            if STATE["loss_streak"] >= STOP_AFTER_SL_STREAK:
                                STATE["halt_for_today"] = True
                                telegram_send(f"🛑 Halted for today: SL streak {STATE['loss_streak']}. Trading paused until tomorrow.")
                    except Exception as e:
                        print("Close error:", e)
                    finally:
                        # reset active
                        STATE.update({
                            "active_symbol": None,
                            "entry_side": None,
                            "entry_price_1": None,
                            "entry_amount": 0.0,
                            "safety_used": False,
                            "sl_price": None,
                            "basket_tp_usdt": None,
                        })

                time.sleep(POLL_SECONDS)
                continue

            # No active position -> scan new signals (respect daily limits)
            if STATE["trades_today"] >= MAX_TRADES_PER_DAY:
                time.sleep(POLL_SECONDS); continue

            candidates = []
            for sym in SYMBOLS:
                try:
                    data = fetch_ohlcv(ex, sym, TIMEFRAME, limit=ATR_PERIOD*4 + EMA_TREND + 5)
                    c = data["close"]; h=data["high"]; l=data["low"]
                    e9,e21,e50,a = indicators(data)
                    if not sideway_filter(e9,e21,e50,a,c): 
                        continue
                    sig = generate_signal(e9,a,c)
                    if not sig: 
                        continue
                    price = ticker(ex, sym)
                    atr_val = a[-1]
                    if math.isnan(atr_val) or atr_val <= 0: 
                        continue
                    candidates.append({"symbol": sym, "signal": sig, "atr": atr_val, "price": price})
                except Exception as e:
                    print(f"Scan error {sym}:", e)

            best = pick_best_candidate(candidates)  # Rank A
            if best is None:
                time.sleep(POLL_SECONDS); continue

            symbol = best["symbol"]; sig = best["signal"]; price = best["price"]; atr_val = best["atr"]

            # SL absolute (TP ใช้ basket/logic)
            if sig == "long":
                sl_price = price - SL_ATR * atr_val
                side = 'buy'
            else:
                sl_price = price + SL_ATR * atr_val
                side = 'sell'

            # Sizing
            if FIXED_NOTIONAL_USDT > 0:
                notional = FIXED_NOTIONAL_USDT
            else:
                equity = max(get_equity_usdt(ex), 10.0)
                move_pct = abs(sl_price - price) / price
                if move_pct <= 0:
                    time.sleep(POLL_SECONDS); continue
                notional = equity * RISK_PER_TRADE  # ความเสี่ยง (USDT) = notional * move_pct  -> notional = risk/move_pct
                notional = notional / max(move_pct, 1e-9)

            amount_base = notional / price
            amount_base = amount_to_precision(ex, symbol, amount_base)
            amount_base = enforce_min_amount(ex, symbol, amount_base)
            if amount_base <= 0:
                time.sleep(POLL_SECONDS); continue

            try:
                ex.create_order(symbol, 'market', side, amount_base)
                STATE["trades_today"] += 1
                STATE.update({
                    "active_symbol": symbol,
                    "entry_side": sig,
                    "entry_price_1": price,
                    "entry_amount": amount_base,
                    "safety_used": False,
                    "sl_price": sl_price,
                    "basket_tp_usdt": BASKET_TP_ATR * atr_val * amount_base * price,
                })
            except Exception as e:
                print("Open order error:", e)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            print("Loop error:", e)
            traceback.print_exc()
            time.sleep(3)

if __name__ == "__main__":
    run()
