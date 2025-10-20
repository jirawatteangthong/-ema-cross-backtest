#!/usr/bin/env python3
"""
OKX EMA Sideway Scalper (Market orders) — v1.0
- Sideway filter with EMA9/21/50 + ATR
- Mean-reversion entries around EMA9
- TP/SL in ATR units
- Single DCA (fixed size), Basket TP
- Max 10 trades/day
- Isolated, leverage configurable
- Daily summary to Telegram (once/day)

Environment variables (with defaults for local testing):
  OKX_API_KEY, OKX_SECRET, OKX_PASSWORD
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Optional:
  SYMBOL=BTC/USDT:USDT
  TIMEFRAME=5m
  LEVERAGE=10
  RISK_PER_TRADE=0.01          # 1% of equity
  MAX_TRADES_PER_DAY=10
  DAILY_SUMMARY_HOUR=23        # Asia/Bangkok hour 0-23
  DAILY_SUMMARY_MINUTE=55
  POLL_SECONDS=5
"""

import os
import time
import math
import json
import traceback
from datetime import datetime
import pytz
import requests

import ccxt
import numpy as np

# -------------------- Config --------------------
API_KEY = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY')
SECRET = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET')
PASSWORD = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSPHRASE')

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID')

SYMBOL = os.getenv('SYMBOL', 'BTC/USDT:USDT')
TIMEFRAME = os.getenv('TIMEFRAME', '5m')
LEVERAGE = int(os.getenv('LEVERAGE', '10'))
RISK_PER_TRADE = float(os.getenv('RISK_PER_TRADE', '0.01'))
MAX_TRADES_PER_DAY = int(os.getenv('MAX_TRADES_PER_DAY', '10'))
POLL_SECONDS = int(os.getenv('POLL_SECONDS', '5'))

DAILY_SUMMARY_HOUR = int(os.getenv('DAILY_SUMMARY_HOUR', '23'))     # Bangkok time
DAILY_SUMMARY_MINUTE = int(os.getenv('DAILY_SUMMARY_MINUTE', '55'))

# Strategy params
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 50
ATR_PERIOD = 14

# Filters
EMA_GAP_MAX = 0.001   # 0.10%
ATR_PCT_MIN = 0.002   # 0.20%
ATR_PCT_MAX = 0.008   # 0.80%
EMA50_SLOPE_MAX = 0.0003  # ~0.03% per bar

# Entry/Exit params (in ATR units)
EXTENSION_ATR = 0.35      # price extends this far beyond EMA9
TP_ATR = 0.25
SL_ATR = 0.55
DCA_TRIGGER_ATR = 0.30
BASKET_TP_ATR = 0.10

BANGKOK = pytz.timezone('Asia/Bangkok')

STATE = {
    "today": None,
    "trades_today": 0,
    "summary": {
        "wins": 0, "losses": 0, "closed_pnl_usdt": 0.0, "trades": 0
    },
    "sent_summary_for": None,
}

# -------------------- Helpers --------------------
def ema(arr, period):
    if len(arr) < period:
        return np.array([np.nan] * len(arr))
    k = 2/(period+1)
    out = np.zeros_like(arr, dtype=float)
    out[:] = np.nan
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
    if len(c) < period+1:
        return out
    out[period] = np.nanmean(tr[1:period+1])
    for i in range(period+1, len(c)):
        out[i] = (out[i-1]*(period-1) + tr[i]) / period
    return out

def telegram_send(text):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == 'YOUR_TELEGRAM_TOKEN':
        print("[TELEGRAM] (skip) ", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode":"HTML"}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

def now_bkk():
    return datetime.now(BANGKOK)

def same_day(a: datetime, b: datetime):
    return a.date() == b.date()

def ensure_new_day():
    """Rotate counters if day changed."""
    global STATE
    n = now_bkk()
    if STATE["today"] is None or not same_day(n, STATE["today"]):
        STATE["today"] = n
        STATE["trades_today"] = 0
        STATE["summary"] = {"wins":0, "losses":0, "closed_pnl_usdt":0.0, "trades":0}
        # reset daily summary flag so a new day's summary can be sent
        STATE["sent_summary_for"] = None

def maybe_send_daily_summary(force=False):
    n = now_bkk()
    ensure_new_day()
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

def amount_to_precision(exchange, symbol, amount):
    return float(exchange.amount_to_precision(symbol, amount))

# -------------------- Exchange --------------------
def create_exchange():
    exchange = ccxt.okx({
        "apiKey": API_KEY,
        "secret": SECRET,
        "password": PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",   # USDT perpetual
            "positionSide": "net"    # important for Net mode
        }
    })
    exchange.load_markets()
    try:
        # Set isolated margin mode with Net position
        exchange.set_margin_mode("isolated", SYMBOL, params={"posSide": "net"})
    except Exception as e:
        print("Set margin mode failed (continue):", str(e))
    try:
        # Set leverage (1 - 125 allowed on OKX futures)
        exchange.set_leverage(LEVERAGE, SYMBOL, params={"mgnMode": "isolated", "posSide": "net"})
    except Exception as e:
        print("Set leverage failed (continue):", str(e))
    return exchange
# -------------------- Strategy Core --------------------
def fetch_ohlcv(exchange, symbol, timeframe, limit=200):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    o, h, l, c, t = [], [], [], [], []
    for x in ohlcv:
        t.append(x[0])
        o.append(x[1]); h.append(x[2]); l.append(x[3]); c.append(x[4])
    return {
        "ts": np.array(t, dtype=np.int64),
        "open": np.array(o, dtype=float),
        "high": np.array(h, dtype=float),
        "low": np.array(l, dtype=float),
        "close": np.array(c, dtype=float),
    }

def indicators(data):
    c = data["close"]; h = data["high"]; l = data["low"]
    e9 = ema(c, EMA_FAST)
    e21 = ema(c, EMA_SLOW)
    e50 = ema(c, EMA_TREND)
    a = atr(h, l, c, ATR_PERIOD)
    return e9, e21, e50, a

def sideway_filter(e9, e21, e50, atr_vals, c):
    i = -1
    if any(map(lambda arr: len(arr) < 2 or np.isnan(arr[i]), [e9,e21,e50,atr_vals])):
        return False
    price = c[i]
    gap = abs(e9[i] - e21[i]) / price               # EMA9-21 gap %
    atr_pct = atr_vals[i] / price                    # ATR %
    slope = abs(e50[i] - e50[i-1]) / price          # EMA50 slope ~%
    return (gap <= EMA_GAP_MAX) and (ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX) and (slope <= EMA50_SLOPE_MAX)

def generate_signal(e9, atr_vals, c):
    i = -1
    if np.isnan(e9[i]) or np.isnan(atr_vals[i]):
        return None
    price = c[i]
    prev_price = c[i-1]
    prev_e9 = e9[i-1]
    # Long: price extended below EMA9 and closed back above EMA9
    if prev_price < (prev_e9 - EXTENSION_ATR * atr_vals[i-1]) and price > e9[i]:
        return "long"
    # Short: extended above and close back below
    if prev_price > (prev_e9 + EXTENSION_ATR * atr_vals[i-1]) and price < e9[i]:
        return "short"
    return None

# -------------------- Position & Orders --------------------
def get_equity_usdt(exchange):
    bal = exchange.fetch_balance(params={"type":"swap"})
    usdt = bal.get("USDT", {})
    eq = usdt.get("total", None)
    if eq is None:
        # fallback to info
        try:
            eq = float(usdt.get("info", {}).get("eq", 0))
        except Exception:
            eq = 0
    return float(eq or 0.0)

def position_size_from_risk(exchange, symbol, side, entry_price, sl_price, risk_fraction):
    equity = max(get_equity_usdt(exchange), 30.0)  # guard for start
    risk_usdt = equity * risk_fraction
    move_pct = abs(sl_price - entry_price) / entry_price
    if move_pct <= 0:
        return 0.0
    notional = risk_usdt / move_pct  # position value in USDT
    amount_base = notional / entry_price
    amount_base = amount_to_precision(exchange, symbol, amount_base)
    return amount_base

def fetch_ticker_price(exchange, symbol):
    t = exchange.fetch_ticker(symbol)
    return float(t['last'])

def close_all_positions(exchange, symbol):
    # market-close by placing opposite side with size
    positions = exchange.fetch_positions([symbol])
    for p in positions:
        if p.get('symbol') != symbol:
            continue
        # ccxt okx normalizes either 'contracts' or 'size'
        amt = float(p.get('contracts') or p.get('size') or 0)
        side = (p.get('side') or '').lower()
        if amt and side:
            try:
                if side == 'long':
                    exchange.create_order(symbol, 'market', 'sell', amt)
                else:
                    exchange.create_order(symbol, 'market', 'buy', amt)
            except Exception as e:
                print("close_all error:", e)

def get_open_position(exchange, symbol):
    positions = exchange.fetch_positions([symbol])
    for p in positions:
        if p.get('symbol') == symbol:
            amt = float(p.get('contracts') or p.get('size') or 0)
            if amt != 0:
                return p
    return None

def unrealized_pnl_usdt(pos):
    if not pos:
        return 0.0
    # ccxt normalized okx uses 'unrealizedPnl' USDT
    try:
        return float(pos.get('unrealizedPnl', 0.0))
    except Exception:
        return 0.0

# -------------------- Main loop --------------------
def run():
    exchange = create_exchange()
    print("Markets loaded. Running strategy on", SYMBOL, TIMEFRAME)
    ensure_new_day()

    safety_used = False
    entry_price_1 = None
    entry_side = None
    entry_amount = 0.0
    basket_tp_usdt = None
    sl_price = None

    while True:
        try:
            ensure_new_day()
            # send summary if time
            maybe_send_daily_summary(force=False)

            if STATE["trades_today"] >= MAX_TRADES_PER_DAY:
                time.sleep(POLL_SECONDS)
                continue

            data = fetch_ohlcv(exchange, SYMBOL, TIMEFRAME, limit=ATR_PERIOD*4 + EMA_TREND + 5)
            c = data["close"]; h=data["high"]; l=data["low"]
            e9,e21,e50,a = indicators(data)
            if not sideway_filter(e9,e21,e50,a,c):
                time.sleep(POLL_SECONDS)
                continue

            sig = generate_signal(e9,a,c)
            if sig and get_open_position(exchange, SYMBOL) is None:
                # Determine entry
                price = fetch_ticker_price(exchange, SYMBOL)

                # compute TP/SL absolute
                atr_val = a[-1]
                if math.isnan(atr_val) or atr_val <= 0:
                    time.sleep(POLL_SECONDS); continue

                if sig == "long":
                    tp_price = price + TP_ATR * atr_val
                    sl_price = price - SL_ATR * atr_val
                    side = 'buy'
                else:
                    tp_price = price - TP_ATR * atr_val
                    sl_price = price + SL_ATR * atr_val
                    side = 'sell'

                # size from risk
                amt = position_size_from_risk(exchange, SYMBOL, sig, price, sl_price, RISK_PER_TRADE)
                if amt <= 0:
                    time.sleep(POLL_SECONDS); continue

                # Send market order
                exchange.create_order(SYMBOL, 'market', side, amt)
                STATE["trades_today"] += 1
                entry_price_1 = price
                entry_amount = amt
                entry_side = sig
                safety_used = False
                basket_tp_usdt = BASKET_TP_ATR * atr_val * amt * price  # approx in USDT
                telegram_send(f"🚀 Open {sig.upper()} {SYMBOL}\nAmt: {amt}\nEntry: {price:.2f}\nTP≈{tp_price:.2f} SL≈{sl_price:.2f}")

            # Monitor open position for TP/SL/DCA/Basket TP
            pos = get_open_position(exchange, SYMBOL)
            if pos and entry_side:
                price = fetch_ticker_price(exchange, SYMBOL)

                # DCA if adverse move
                if not safety_used and entry_price_1 is not None and a[-1] and not math.isnan(a[-1]):
                    adverse_ok = (price <= entry_price_1 - DCA_TRIGGER_ATR*a[-1]) if entry_side == 'long' else (price >= entry_price_1 + DCA_TRIGGER_ATR*a[-1])
                    if adverse_ok:
                        try:
                            side2 = 'buy' if entry_side == 'long' else 'sell'
                            exchange.create_order(SYMBOL, 'market', side2, entry_amount)  # same size
                            safety_used = True
                            telegram_send(f"🧩 DCA executed on {SYMBOL}. Added {entry_amount}")
                        except Exception as e:
                            print("DCA error:", e)

                # Basket TP or SL logic via unrealized PnL
                pos = get_open_position(exchange, SYMBOL)  # refresh
                pnl = unrealized_pnl_usdt(pos) if pos else 0.0

                hit_basket_tp = basket_tp_usdt is not None and pnl >= basket_tp_usdt
                hit_sl = False
                if entry_side == 'long' and sl_price is not None and price <= sl_price:
                    hit_sl = True
                if entry_side == 'short' and sl_price is not None and price >= sl_price:
                    hit_sl = True

                if hit_basket_tp or hit_sl:
                    try:
                        # Close by market
                        close_all_positions(exchange, SYMBOL)
                        result = "✅ TP (Basket)" if hit_basket_tp else "🛑 SL"
                        pnl_final = pnl
                        if pnl_final >= 0: STATE["summary"]["wins"] += 1
                        else: STATE["summary"]["losses"] += 1
                        STATE["summary"]["trades"] += 1
                        STATE["summary"]["closed_pnl_usdt"] += pnl_final
                        telegram_send(f"{result} {SYMBOL}\nPNL: {pnl_final:.2f} USDT")
                    except Exception as e:
                        print("Close error:", e)
                    finally:
                        # reset
                        safety_used = False
                        entry_price_1 = None
                        entry_side = None
                        entry_amount = 0.0
                        basket_tp_usdt = None
                        sl_price = None

            time.sleep(POLL_SECONDS)

        except Exception as e:
            print("Loop error:", e)
            traceback.print_exc()
            time.sleep(3)

if __name__ == "__main__":
    run()
