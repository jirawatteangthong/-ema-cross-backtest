#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX EMA Sideway Scalper — FINAL (Small Capital Edition)
- Symbols: DOGE/USDT:USDT, TRX/USDT:USDT
- Timeframe: 5m
- Isolated + Net mode, Leverage 15x
- Fixed notional per order = 0.5 USDT (คงที่จริง ไม่ผูก ATR)
- Sideway filter (EMA9/21 gap + ATR% + EMA50 slope)
- Mean reversion around EMA9
- TP/SL by ATR, DCA 1 ไม้, Basket TP
- Max 20 trades/day
- Stop after 5 consecutive SL (halt until next day) + Daily summary 23:55
- Single position system (เปิดได้ทีละ 1 คู่)

ENV REQUIRED:
  OKX_API_KEY, OKX_SECRET, OKX_PASSWORD
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

import os, time, math, traceback
from datetime import datetime
import pytz, requests
import ccxt
import numpy as np

# ====== ENV (เฉพาะ Exchange + Telegram) ======
API_KEY = os.getenv('OKX_API_KEY', '')
SECRET = os.getenv('OKX_SECRET', '')
PASSWORD = os.getenv('OKX_PASSWORD', '')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# ====== FIXED SETTINGS (ตามที่ยืนยัน) ======
SYMBOLS = ["DOGE/USDT:USDT", "TRX/USDT:USDT"]
TIMEFRAME = "5m"
LEVERAGE = 15                 # คุณยืนยัน 15x
MARGIN_MODE = "isolated"      # Isolated + Net

# ขนาดต่อไม้แบบคงที่จริง (USDT notional) — สำคัญที่สุดสำหรับทุนเล็ก
FIXED_NOTIONAL_USDT = 0.5     # ถ้าจะปรับให้ใหญ่ขึ้นค่อยแก้ตัวเลขนี้
# ไม่ใช้ risk mode เพื่อไม่ให้ขนาดแกว่งตาม ATR
RISK_PER_TRADE = 0.0

POLL_SECONDS = 5
MAX_TRADES_PER_DAY = 20
STOP_AFTER_SL_STREAK = 5

DAILY_SUMMARY_HOUR = 23
DAILY_SUMMARY_MINUTE = 55

# Indicators/Filters
EMA_FAST, EMA_SLOW, EMA_TREND = 9, 21, 50
ATR_PERIOD = 14
EMA_GAP_MAX = 0.001          # 0.10%
ATR_PCT_MIN, ATR_PCT_MAX = 0.002, 0.008
EMA50_SLOPE_MAX = 0.0003     # ~0.03%/bar

# Entries/Exits (ATR units)
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
    # Active position (single position system)
    "active_symbol": None,
    "entry_side": None,        # "long"/"short"
    "entry_price_1": None,
    "entry_amount": 0.0,
    "safety_used": False,
    "sl_price": None,
    "basket_tp_usdt": None,
}

# ====== Helpers ======
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
        print("[TELEGRAM] (skip)", text); return
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
    should_time = (n.hour==DAILY_SUMMARY_HOUR and n.minute>=DAILY_SUMMARY_MINUTE)
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

# ====== Exchange ======
def create_exchange():
    ex = ccxt.okx({
        "apiKey": API_KEY,
        "secret": SECRET,
        "password": PASSWORD,
        "enableRateLimit": True,
        "options": {"defaultType": "swap", "positionSide": "net"}
    })
    ex.load_markets()
    for sym in SYMBOLS:
        try:
            # set isolated (ไม่ต้องส่ง lever ในฟังก์ชันนี้ ป้องกัน warning)
            ex.set_margin_mode(MARGIN_MODE, sym)
        except Exception as e:
            print(f"Set margin mode failed ({sym}):", e)
        try:
            # กำหนด leverage พร้อม mgnMode/posSide ให้ชัด
            ex.set_leverage(LEVERAGE, sym, params={"mgnMode":"isolated","posSide":"net"})
        except Exception as e:
            print(f"Set leverage failed ({sym}):", e)
    return ex

def fetch_ohlcv(ex, symbol, timeframe, limit=200):
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    o,h,l,c,t = [],[],[],[],[]
    for x in ohlcv:
        t.append(x[0]); o.append(x[1]); h.append(x[2]); l.append(x[3]); c.append(x[4])
    return {"ts":np.array(t), "open":np.array(o), "high":np.array(h), "low":np.array(l), "close":np.array(c)}

def indicators(data):
    c,h,l = data["close"], data["high"], data["low"]
    e9 = ema(c, EMA_FAST); e21 = ema(c, EMA_SLOW); e50 = ema(c, EMA_TREND)
    a = atr(h, l, c, ATR_PERIOD)
    return e9,e21,e50,a

def sideway_filter(e9,e21,e50,a,c):
    i=-1
    if any(len(arr)<2 or np.isnan(arr[i]) for arr in [e9,e21,e50,a]): return False
    price = float(c[i])
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

def amount_to_precision(ex, symbol, amount):
    return float(ex.amount_to_precision(symbol, amount))

def enforce_min_amount(ex, symbol, amount):
    m = ex.market(symbol)
    try:
        min_amt = m.get('limits', {}).get('amount', {}).get('min', None)
        if min_amt is not None and amount < float(min_amt):
            return float(min_amt)
    except: pass
    return float(amount)

def ticker(ex, symbol):
    return float(ex.fetch_ticker(symbol)['last'])

def fetch_positions_map(ex, symbols):
    pos = ex.fetch_positions(symbols)
    mp = {s: None for s in symbols}
    for p in pos:
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
                ex.create_order(symbol, 'market', ('sell' if side=='long' else 'buy'), amt)
            except Exception as e:
                print("close_all error:", e)

# ----- Robust order sender: ลดขนาดครึ่งหนึ่งอัตโนมัติถ้า margin ไม่พอ -----
def place_market_with_retries(ex, symbol, side, amount):
    """
    ส่งคำสั่งตลาด ถ้าโดน insufficient margin จะลดครึ่งแล้วลองใหม่
    หยุดเมื่อต่ำกว่า min-amount ของตลาด
    """
    amount = amount_to_precision(ex, symbol, amount)
    amount = enforce_min_amount(ex, symbol, amount)
    while amount > 0:
        try:
            return ex.create_order(symbol, 'market', side, amount)
        except Exception as e:
            msg = str(e)
            if 'Insufficient' in msg or 'insufficient' in msg or '51008' in msg:
                amount = amount/2.0
                amount = amount_to_precision(ex, symbol, amount)
                m = ex.market(symbol)
                min_amt = m.get('limits', {}).get('amount', {}).get('min', 0) or 0
                if amount <= float(min_amt):
                    print(f"Order skipped: amount below min after retries ({amount})")
                    return None
                print(f"Retry with smaller amount: {amount}")
                time.sleep(0.5)
                continue
            else:
                print("Open order error (non-margin):", e)
                return None

# ====== Ranking (Rank A = ATR เล็กสุด) ======
def pick_best_candidate(cands):
    if not cands: return None
    return min(cands, key=lambda x: x["atr"])

# ====== Main ======
def run():
    ex = create_exchange()
    print("Markets loaded. Multi-symbol on", SYMBOLS, TIMEFRAME)
    ensure_new_day()

    while True:
        try:
            ensure_new_day()
            maybe_send_daily_summary(False)

            if STATE["halt_for_today"]:
                time.sleep(POLL_SECONDS); continue

            # Active position?
            pos_map = fetch_positions_map(ex, SYMBOLS)
            active = None
            for sym, p in pos_map.items():
                if p is not None: active = sym; break

            # Manage active
            if active or STATE["active_symbol"]:
                symbol = active or STATE["active_symbol"]
                if STATE["active_symbol"] is None:
                    STATE["active_symbol"] = symbol

                price = ticker(ex, symbol)

                # DCA once
                if (not STATE["safety_used"]) and (STATE["entry_price_1"] is not None):
                    data = fetch_ohlcv(ex, symbol, TIMEFRAME, limit=EMA_TREND+ATR_PERIOD+5)
                    e9,e21,e50,a = indicators(data)
                    if len(a)>0 and not math.isnan(a[-1]):
                        adverse = (price <= STATE["entry_price_1"] - DCA_TRIGGER_ATR*a[-1]) if STATE["entry_side"]=='long' else (price >= STATE["entry_price_1"] + DCA_TRIGGER_ATR*a[-1])
                        if adverse:
                            side2 = 'buy' if STATE["entry_side"]=='long' else 'sell'
                            place_market_with_retries(ex, symbol, side2, STATE["entry_amount"])
                            STATE["safety_used"] = True

                # Basket TP/SL
                p = fetch_positions_map(ex, [symbol]).get(symbol)
                pnl = unrealized_pnl_usdt(p)
                hit_basket_tp = (STATE["basket_tp_usdt"] is not None) and (pnl >= STATE["basket_tp_usdt"])
                hit_sl = False
                if STATE["entry_side"]=='long' and STATE["sl_price"] is not None and price <= STATE["sl_price"]: hit_sl = True
                if STATE["entry_side"]=='short' and STATE["sl_price"] is not None and price >= STATE["sl_price"]: hit_sl = True

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
                            if STATE["loss_streak"] >= STOP_AFTER_SL_STREAK:
                                STATE["halt_for_today"] = True
                                telegram_send(f"🛑 Halted today: SL streak {STATE['loss_streak']}. Resume tomorrow.")
                    except Exception as e:
                        print("Close error:", e)
                    finally:
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

            # No active -> scan
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
                    if math.isnan(atr_val) or atr_val<=0:
                        continue
                    candidates.append({"symbol":sym,"signal":sig,"atr":float(atr_val),"price":float(price)})
                except Exception as e:
                    print(f"Scan error {sym}:", e)

            best = pick_best_candidate(candidates)
            if best is None:
                time.sleep(POLL_SECONDS); continue

            symbol, sig, price, atr_val = best["symbol"], best["signal"], best["price"], best["atr"]

            # SL (TP จะใช้ basket logic)
            if sig == "long":
                sl_price = price - SL_ATR * atr_val
                side = 'buy'
            else:
                sl_price = price + SL_ATR * atr_val
                side = 'sell'

            # --- Fixed notional sizing (คงที่จริง 0.5 USDT) ---
            notional = FIXED_NOTIONAL_USDT
            amount_base = max( notional / price, 0.0 )
            amount_base = amount_to_precision(ex, symbol, amount_base)
            amount_base = enforce_min_amount(ex, symbol, amount_base)
            if amount_base <= 0:
                time.sleep(POLL_SECONDS); continue

            # ส่งคำสั่งด้วยฟังก์ชันที่ลดขนาดอัตโนมัติถ้า margin ไม่พอ
            res = place_market_with_retries(ex, symbol, side, amount_base)
            if res is None:
                # เปิดไม่ได้ (ส่วนใหญ่เพราะต่ำกว่าขั้นต่ำหลังลดครึ่ง) -> ข้าม
                time.sleep(POLL_SECONDS); continue

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

            time.sleep(POLL_SECONDS)

        except Exception as e:
            print("Loop error:", e)
            traceback.print_exc()
            time.sleep(3)

if __name__ == "__main__":
    run()
