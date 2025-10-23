#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MINI EMA Bot — OKX Futures (One-way/Cross) — ETH-USDT-SWAP
TF: 5m | Entry: EMA 9/21 cross | TP/SL: ±5% (RR 1:1)
Lot: fixed 15 USDT per trade
Notify: Telegram only on start + open-fail (once), and Daily Summary 23:55
"""

import os, time, math, traceback
from datetime import datetime
import pytz, requests
import numpy as np
import ccxt

# ====== ENV ======
API_KEY = os.getenv('OKX_API_KEY', '')
SECRET = os.getenv('OKX_SECRET', '')
PASSWORD = os.getenv('OKX_PASSWORD', '')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# ====== CONFIG ======
SYMBOL = "ETH-USDT-SWAP"
TIMEFRAME = "5m"
LEVERAGE = 25
MARGIN_MODE = "cross"     # cross = เปิดง่ายสุด
POSITION_SIDE = "net"     # one-way
LOOP_SLEEP = 5            # seconds
LOT_USDT = 15.0           # notional per order (fixed)
TP_PCT = 0.05             # +5%
SL_PCT = 0.05             # -5%

# Summary time (Bangkok)
BKK = pytz.timezone('Asia/Bangkok')
DAILY_SUMMARY_HOUR = 23
DAILY_SUMMARY_MINUTE = 55

# ====== STATE ======
STATE = {
    "today": None,
    "sent_summary_for": None,
    "open_fail_notified_for": None,   # date string when last fail notified
    "summary": {"trades":0, "wins":0, "losses":0, "tp":0, "sl":0, "pnl":0.0},
    # position
    "side": None,         # "long"/"short"
    "entry": None,        # float
    "size": 0.0,          # amount in base (ETH)
    "tp_price": None,
    "sl_price": None,
}

# ====== Utils ======
def now_bkk(): return datetime.now(BKK)
def same_day(a,b): return a.date()==b.date()

def telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: 
        print("[TG] (skip) " + text); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode":"HTML"},
            timeout=10
        )
    except Exception as e:
        print("Telegram error:", e)

def ensure_new_day():
    n = now_bkk()
    if STATE["today"] is None or not same_day(n, STATE["today"]):
        STATE["today"] = n
        STATE["sent_summary_for"] = None
        STATE["open_fail_notified_for"] = None
        STATE["summary"] = {"trades":0, "wins":0, "losses":0, "tp":0, "sl":0, "pnl":0.0}

def maybe_send_daily_summary():
    n = now_bkk(); ensure_new_day()
    key = n.strftime("%Y-%m-%d")
    if (n.hour==DAILY_SUMMARY_HOUR and n.minute>=DAILY_SUMMARY_MINUTE and STATE["sent_summary_for"] != key):
        s = STATE["summary"]
        winrate = (s["wins"]/s["trades"]*100.0) if s["trades"]>0 else 0.0
        msg = (f"📊 <b>Daily Summary ({key})</b>\n"
               f"Pair: ETH-USDT-SWAP\n"
               f"Timeframe: 5m\n"
               f"Trades: {s['trades']}\n"
               f"Wins: {s['wins']}\n"
               f"Loss: {s['losses']}\n"
               f"Winrate: {winrate:.0f}%\n"
               f"TP: {s['tp']} ครั้ง\n"
               f"SL: {s['sl']} ครั้ง\n"
               f"PNL: {s['pnl']:+.2f} USDT\n"
               f"Status: Bot Running ✅")
        telegram(msg)
        STATE["sent_summary_for"] = key

# ====== Indicators ======
def ema(arr, period):
    if len(arr) < period: return np.array([np.nan]*len(arr))
    k = 2/(period+1)
    out = np.empty_like(arr, dtype=float); out[:] = np.nan
    out[period-1] = np.mean(arr[:period])
    for i in range(period, len(arr)):
        out[i] = arr[i]*k + out[i-1]*(1-k)
    return out

def cross_up(e1,e2):   return e1[-2] <= e2[-2] and e1[-1] > e2[-1]
def cross_down(e1,e2): return e1[-2] >= e2[-2] and e1[-1] < e2[-1]

# ====== Exchange ======
def create_exchange():
    ex = ccxt.okx({
        "apiKey": API_KEY,
        "secret": SECRET,
        "password": PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "positionSide": POSITION_SIDE,
        },
        "timeout": 30000,
    })
    markets = ex.load_markets()
    # Margin/Leverage (cross + one-way)
    try: ex.set_margin_mode(MARGIN_MODE, SYMBOL)
    except Exception as e: print("set_margin_mode:", e)
    try: ex.set_leverage(LEVERAGE, SYMBOL, params={"mgnMode":"cross","posSide":"net"})
    except Exception as e: print("set_leverage:", e)
    return ex

def fetch_ohlcv(ex, limit=200):
    return ex.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=limit)

def price(ex): 
    return float(ex.fetch_ticker(SYMBOL)['last'])

def equity_usdt(ex):
    bal = ex.fetch_balance(params={"type":"swap"})
    usdt = bal.get("USDT", {})
    eq = usdt.get("total")
    if eq is None:
        try: eq = float(usdt.get("info",{}).get("eq", 0))
        except: eq = 0
    return float(eq or 0.0)

def enforce_min_amount(ex, amount):
    m = ex.market(SYMBOL)
    min_amt = (m.get('limits',{}).get('amount',{}) or {}).get('min', None)
    if min_amt is not None and amount < float(min_amt):
        return float(min_amt)
    return amount

def open_market(ex, side, notional_usdt):
    """return order|None"""
    px = price(ex)
    amt = max(notional_usdt/px, 0.0)
    amt = float(ex.amount_to_precision(SYMBOL, amt))
    amt = enforce_min_amount(ex, amt)
    if amt <= 0:
        return None
    try:
        return ex.create_order(SYMBOL, 'market', ('buy' if side=='long' else 'sell'), amt, params={"tdMode":"cross"})
    except Exception as e:
        print("open_market error:", e)
        return None

def close_all(ex, side):
    try:
        # reduceOnly market to flatten
        amt = 0.0
        pos = ex.fetch_positions([SYMBOL])
        for p in pos:
            if p.get('symbol')==SYMBOL:
                amt = float(p.get('contracts') or p.get('size') or 0)
                break
        if amt>0:
            ex.create_order(SYMBOL, 'market', ('sell' if side=='long' else 'buy'), amt, params={"tdMode":"cross","reduceOnly":True})
    except Exception as e:
        print("close_all error:", e)

# ====== Main loop ======
def run():
    ex = create_exchange()
    ensure_new_day()
    telegram("🤖 MINI EMA Bot started.\nPair: ETH-USDT-SWAP | TF: 5m | Lot: 15 USDT | TP/SL: 5%")

    # one-time open-fail notify control
    def notify_open_fail_once(msg):
        key = now_bkk().strftime("%Y-%m-%d")
        if STATE["open_fail_notified_for"] != key:
            telegram(msg)
            STATE["open_fail_notified_for"] = key

    while True:
        try:
            ensure_new_day()
            maybe_send_daily_summary()

            # If no position → look for signal
            if STATE["side"] is None:
                ohlcv = fetch_ohlcv(ex, limit=210)
                closes = np.array([x[4] for x in ohlcv], dtype=float)
                if len(closes) < 55:
                    time.sleep(LOOP_SLEEP); continue

                e9  = ema(closes, 9)
                e21 = ema(closes, 21)

                sig = None
                if cross_up(e9, e21):
                    sig = "long"
                elif cross_down(e9, e21):
                    sig = "short"

                if sig:
                    # open
                    od = open_market(ex, sig, LOT_USDT)
                    if od is None or not od.get('id'):
                        notify_open_fail_once("⛔️ เปิดออเดอร์ไม่สำเร็จ (ครั้งเดียววันนี้). ตรวจสอบ API/Permission/ขั้นต่ำใน OKX")
                        time.sleep(LOOP_SLEEP); continue

                    # fetch entry & size
                    px = price(ex)
                    STATE["side"] = sig
                    STATE["entry"] = px
                    STATE["size"] = float(od.get('amount') or 0.0) or float(ex.amount_to_precision(SYMBOL, LOT_USDT/px))
                    if sig=='long':
                        STATE["tp_price"] = STATE["entry"]*(1+TP_PCT)
                        STATE["sl_price"] = STATE["entry"]*(1-SL_PCT)
                    else:
                        STATE["tp_price"] = STATE["entry"]*(1-TP_PCT)
                        STATE["sl_price"] = STATE["entry"]*(1+SL_PCT)
            else:
                # manage TP/SL (no telegram here by request)
                px = price(ex)
                hit_tp = False; hit_sl = False
                if STATE["side"]=='long':
                    hit_tp = px >= STATE["tp_price"]
                    hit_sl = px <= STATE["sl_price"]
                else:
                    hit_tp = px <= STATE["tp_price"]
                    hit_sl = px >= STATE["sl_price"]

                if hit_tp or hit_sl:
                    # close & update summary
                    side = STATE["side"]
                    entry = STATE["entry"]
                    close_all(ex, side)
                    # pnl est in USDT using size * price diff
                    # approximate base amount from LOT_USDT/entry
                    amt_base = STATE["size"] or (LOT_USDT/entry)
                    pnl = 0.0
                    if side=='long':
                        pnl = (px - entry) * amt_base
                    else:
                        pnl = (entry - px) * amt_base

                    STATE["summary"]["trades"] += 1
                    if hit_tp:
                        STATE["summary"]["wins"] += 1
                        STATE["summary"]["tp"] += 1
                    else:
                        STATE["summary"]["losses"] += 1
                        STATE["summary"]["sl"] += 1
                    STATE["summary"]["pnl"] += float(pnl)

                    # reset position
                    STATE.update({"side":None,"entry":None,"size":0.0,"tp_price":None,"sl_price":None})

            time.sleep(LOOP_SLEEP)

        except Exception as e:
            print("Loop error:", e)
            traceback.print_exc()
            time.sleep(3)

if __name__ == "__main__":
    run()
