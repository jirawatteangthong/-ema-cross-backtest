#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ETH Ladder EA — EMA 9/21 Cross + EMA50 Filter + ATR Filter
Basket TP/SL: +5% / -5% of equity per set (RR ≈ 1:1)
Auto Portfolio Growth Ladder (max 4 legs):
  20–39:  1 × 15 USDT
  40–59:  2 × 15 USDT
  60–79:  3 × 15 USDT
  80–99:  4 × 15 USDT
  100–199: 4 × 20 USDT
  200–299: 4 × 30 USDT
  300–399: 4 × 40 USDT
  400–499: 4 × 50 USDT
(ขยายขั้นบันไดต่อไปได้)

- Single Position (เปิดได้ชุดเดียวต่อครั้ง, เพิ่มไม้ฝั่งเดียวกันเท่านั้น)
- OKX Futures: Isolated + Net, Leverage 25x
- Timeframe 5m
- Daily Telegram summary 23:55

ENV REQUIRED:
  OKX_API_KEY, OKX_SECRET, OKX_PASSWORD
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

import os, time, math, traceback
from datetime import datetime
import pytz, requests
import numpy as np
import ccxt

# ====== ENV (เฉพาะ Exchange + Telegram) ======
API_KEY = os.getenv('OKX_API_KEY', '')
SECRET = os.getenv('OKX_SECRET', '')
PASSWORD = os.getenv('OKX_PASSWORD', '')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# ====== SETTINGS ======
SYMBOL = "ETH/USDT:USDT"
TIMEFRAME = "5m"
LEVERAGE = 25
MARGIN_MODE = "isolated"
POLL_SECONDS = 5

# Signal filters
EMA_FAST, EMA_SLOW, EMA_TREND = 9, 21, 50
ATR_PERIOD = 14
ATR_PCT_MIN, ATR_PCT_MAX = 0.0015, 0.01  # 0.15%–1.0% of price (TF 5m กลางๆ)
BANGKOK = pytz.timezone('Asia/Bangkok')

# Basket risk/target (ต่อชุด)
BASKET_TARGET_PCT = 0.05  # +5% ของ equity ตอนเริ่มชุด
BASKET_STOP_PCT   = 0.05  # -5% ของ equity ตอนเริ่มชุด

# Daily summary
DAILY_SUMMARY_HOUR = 23
DAILY_SUMMARY_MINUTE = 55

STATE = {
    "today": None,
    "summary": {"wins":0, "losses":0, "trades":0, "closed_pnl_usdt":0.0},
    "sent_summary_for": None,

    # Active basket
    "active_side": None,           # "long"/"short"
    "legs_opened": 0,              # 0..4
    "leg_amounts": [],             # amounts in base (ETH)
    "basket_equity_start": None,   # equity USDT ตอนเริ่มชุด
    "basket_equity_high": None,    # ใช้ต่อยอดภายหลังได้ (ถ้าจะทำ trailing)
}

# ====== Utils ======
def telegram_send(text):
    if not TELEGRAM_TOKEN: print("[TELEGRAM] (skip)", text); return
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
        STATE["summary"] = {"wins":0,"losses":0,"trades":0,"closed_pnl_usdt":0.0}
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

def ema(arr, period):
    if len(arr) < period: return np.array([np.nan]*len(arr))
    k = 2/(period+1)
    out = np.empty_like(arr, dtype=float); out[:] = np.nan
    out[period-1] = np.mean(arr[:period])
    for i in range(period, len(arr)):
        out[i] = arr[i]*k + out[i-1]*(1-k)
    return out

def true_range(h,l,c):
    tr=[np.nan]
    for i in range(1,len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return np.array(tr, dtype=float)

def atr(h,l,c,period=14):
    tr = true_range(h,l,c)
    out = np.array([np.nan]*len(c), dtype=float)
    if len(c) < period+1: return out
    out[period] = np.nanmean(tr[1:period+1])
    for i in range(period+1,len(c)):
        out[i] = (out[i-1]*(period-1) + tr[i]) / period
    return out

# ====== Exchange ======
def create_exchange():
    ex = ccxt.okx({
        "apiKey": API_KEY,
        "secret": SECRET,
        "password": PASSWORD,
        "enableRateLimit": True,
        "options": {"defaultType":"swap","positionSide":"net"}
    })
    ex.load_markets()
    try:
        ex.set_margin_mode(MARGIN_MODE, SYMBOL)
    except Exception as e:
        print("set_margin_mode:", e)
    try:
        ex.set_leverage(LEVERAGE, SYMBOL, params={"mgnMode":"isolated","posSide":"net"})
    except Exception as e:
        print("set_leverage:", e)
    return ex

def amount_to_precision(ex, symbol, amount):
    return float(ex.amount_to_precision(symbol, amount))

def enforce_min_amount(ex, symbol, amount):
    try:
        m = ex.market(symbol)
        min_amt = m.get('limits', {}).get('amount', {}).get('min', None)
        if min_amt is not None and amount < float(min_amt):
            return float(min_amt)
    except: pass
    return float(amount)

def fetch_ohlcv(ex, symbol, timeframe, limit=200):
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    o,h,l,c,t=[],[],[],[],[]
    for x in ohlcv:
        t.append(x[0]); o.append(x[1]); h.append(x[2]); l.append(x[3]); c.append(x[4])
    return {"ts":np.array(t), "open":np.array(o), "high":np.array(h), "low":np.array(l), "close":np.array(c)}

def fetch_balance_equity_usdt(ex):
    bal = ex.fetch_balance(params={"type":"swap"})
    usdt = bal.get("USDT", {})
    eq = usdt.get("total")
    if eq is None:
        try: eq = float(usdt.get("info",{}).get("eq", 0))
        except: eq = 0
    return float(eq or 0.0)

def fetch_positions(ex):
    ps = ex.fetch_positions([SYMBOL])
    for p in ps:
        if p.get('symbol')==SYMBOL:
            amt = float(p.get('contracts') or p.get('size') or 0)
            side = (p.get('side') or '').lower()
            return amt, side if amt!=0 else (0.0, None)
    return 0.0, None

def ticker_price(ex):
    return float(ex.fetch_ticker(SYMBOL)['last'])

def place_market_with_retries(ex, side, amount):
    """
    ลดขนาดครึ่งหนึ่งอัตโนมัติเมื่อ margin ไม่พอ จนถึง min-amount
    """
    amount = amount_to_precision(ex, SYMBOL, amount)
    amount = enforce_min_amount(ex, SYMBOL, amount)
    while amount > 0:
        try:
            return ex.create_order(SYMBOL, 'market', side, amount)
        except Exception as e:
            msg = str(e)
            if 'Insufficient' in msg or 'insufficient' in msg or '51008' in msg:
                amount = amount/2.0
                amount = amount_to_precision(ex, SYMBOL, amount)
                m = ex.market(SYMBOL)
                min_amt = m.get('limits', {}).get('amount', {}).get('min', 0) or 0
                if amount <= float(min_amt):
                    print(f"Order skipped: amount below min after retries ({amount})")
                    return None
                print(f"Retry with smaller amount: {amount}")
                time.sleep(0.3)
                continue
            else:
                print("Open order error:", e)
                return None

# ====== Ladder rules ======
def ladder_leg_usdt_and_max_legs(equity):
    if equity >= 400: return 50.0, 4
    if equity >= 300: return 40.0, 4
    if equity >= 200: return 30.0, 4
    if equity >= 100: return 20.0, 4
    if equity >= 80:  return 15.0, 4
    if equity >= 60:  return 15.0, 3
    if equity >= 40:  return 15.0, 2
    if equity >= 20:  return 15.0, 1
    return 0.0, 0

# ====== Signals ======
def compute_indicators(data):
    c,h,l = data["close"], data["high"], data["low"]
    e9 = ema(c, EMA_FAST); e21 = ema(c, EMA_SLOW); e50 = ema(c, EMA_TREND)
    a = atr(h,l,c, ATR_PERIOD)
    return c,e9,e21,e50,a

def atr_ok(a, c):
    i=-1
    if len(c)==0 or math.isnan(a[i]): return False
    price = float(c[i]); atrp = a[i]/price
    return (ATR_PCT_MIN <= atrp <= ATR_PCT_MAX)

def cross_up(e9,e21):  return (e9[-2] <= e21[-2]) and (e9[-1] > e21[-1])
def cross_down(e9,e21):return (e9[-2] >= e21[-2]) and (e9[-1] < e21[-1])

def entry_signal(c,e9,e21,e50,a):
    i=-1
    if any(np.isnan(x[i]) for x in [e9,e21,e50,a]): return None
    if not atr_ok(a,c): return None
    # Cross + EMA50 direction filter
    if cross_up(e9,e21) and c[i] > e50[i]:
        return "long"
    if cross_down(e9,e21) and c[i] < e50[i]:
        return "short"
    return None

def add_leg_signal(c,e9,e21, side):
    """ยืนยันเดินหน้าต่อ: pullback ผ่าน EMA9 ในทิศทางเดิม"""
    i=-1
    if np.isnan(e9[i]): return False
    if side=='long':
        return c[-2] < e9[-2] and c[-1] > e9[-1] and e9[-1] > e21[-1]
    if side=='short':
        return c[-2] > e9[-2] and c[-1] < e9[-1] and e9[-1] < e21[-1]
    return False

# ====== Basket rules ======
def basket_open(ex):
    eq = fetch_balance_equity_usdt(ex)
    STATE["basket_equity_start"] = eq
    STATE["basket_equity_high"] = eq

def basket_should_close(ex):
    if STATE["basket_equity_start"] is None: return None
    eq_now = fetch_balance_equity_usdt(ex)
    pnl = eq_now - STATE["basket_equity_start"]
    target = BASKET_TARGET_PCT * STATE["basket_equity_start"]
    stop   = -BASKET_STOP_PCT * STATE["basket_equity_start"]
    if pnl >= target: return ("tp", pnl)
    if pnl <= stop:   return ("sl", pnl)
    return None

def reset_basket_state():
    STATE.update({
        "active_side": None,
        "legs_opened": 0,
        "leg_amounts": [],
        "basket_equity_start": None,
        "basket_equity_high": None,
    })

# ====== Main ======
def run():
    ex = create_exchange()
    print("Running ETH Ladder EA on", SYMBOL, TIMEFRAME)
    ensure_new_day()

    while True:
        try:
            ensure_new_day()
            maybe_send_daily_summary(False)

            data = fetch_ohlcv(ex, SYMBOL, TIMEFRAME, limit=EMA_TREND+ATR_PERIOD+10)
            c,e9,e21,e50,a = compute_indicators(data)
            if len(c) < EMA_TREND+ATR_PERIOD: time.sleep(POLL_SECONDS); continue

            # Check basket exit
            if STATE["active_side"]:
                res = basket_should_close(ex)
                if res:
                    typ, pnl = res
                    # Close all market
                    amt, side_on_ex = fetch_positions(ex)
                    if amt and side_on_ex:
                        try:
                            ex.create_order(SYMBOL, 'market', ('sell' if STATE["active_side"]=='long' else 'buy'), amt)
                        except Exception as e:
                            print("Close basket error:", e)
                    # summary
                    STATE["summary"]["trades"] += 1
                    STATE["summary"]["closed_pnl_usdt"] += float(pnl)
                    if typ=="tp": STATE["summary"]["wins"] += 1
                    else: STATE["summary"]["losses"] += 1
                    telegram_send(f"✅ Basket {typ.upper()} | PnL: {pnl:.2f} USDT")
                    reset_basket_state()
                    time.sleep(POLL_SECONDS)
                    continue

            # OPEN FIRST LEG
            if not STATE["active_side"]:
                sig = entry_signal(c,e9,e21,e50,a)
                if sig:
                    equity = fetch_balance_equity_usdt(ex)
                    leg_usdt, max_legs = ladder_leg_usdt_and_max_legs(equity)
                    if leg_usdt <= 0 or max_legs == 0:
                        time.sleep(POLL_SECONDS); continue
                    price = ticker_price(ex)
                    amount = leg_usdt / price
                    amount = amount_to_precision(ex, SYMBOL, amount)
                    amount = enforce_min_amount(ex, SYMBOL, amount)
                    side = 'buy' if sig=='long' else 'sell'
                    res = place_market_with_retries(ex, side, amount)
                    if res:
                        STATE["active_side"] = sig
                        STATE["legs_opened"] = 1
                        STATE["leg_amounts"] = [amount]
                        basket_open(ex)
                        telegram_send(f"🚀 Open 1st leg ({sig.upper()}) {SYMBOL} | {leg_usdt} USDT")
                time.sleep(POLL_SECONDS); continue

            # ADD MORE LEGS (same side) if allowed
            if STATE["active_side"]:
                equity = fetch_balance_equity_usdt(ex)
                leg_usdt, max_legs = ladder_leg_usdt_and_max_legs(equity)
                if STATE["legs_opened"] < max_legs and add_leg_signal(c,e9,e21, STATE["active_side"]):
                    price = ticker_price(ex)
                    amount = leg_usdt / price
                    amount = amount_to_precision(ex, SYMBOL, amount)
                    amount = enforce_min_amount(ex, SYMBOL, amount)
                    side = 'buy' if STATE["active_side"]=='long' else 'sell'
                    res = place_market_with_retries(ex, side, amount)
                    if res:
                        STATE["legs_opened"] += 1
                        STATE["leg_amounts"].append(amount)
                        telegram_send(f"➕ Add leg #{STATE['legs_opened']} ({STATE['active_side'].upper()}) {SYMBOL} | {leg_usdt} USDT")

            time.sleep(POLL_SECONDS)

        except Exception as e:
            print("Loop error:", e)
            traceback.print_exc()
            time.sleep(3)

if __name__ == "__main__":
    run()
