import time
from datetime import datetime, timedelta, timezone
import math

import ccxt
import pandas as pd
import numpy as np

# ===================== CONFIG =====================
SYMBOL = 'BTC/USDT:USDT'   # Binance USDT Perp format in CCXT
TIMEFRAME = '1h'
YEARS = 2                   # Lookback duration
LEVERAGE = 30
START_EQUITY = 100.0        # USDT
RISK_FACTOR = 0.8           # Use 80% of equity as margin similar to TARGET_POSITION_SIZE_FACTOR
MIN_COOLDOWN_BARS = 1       # avoid immediate re-entry same bar

# EMA params (from your bot's defaults)
EMA_FAST = 9
EMA_SLOW = 50
CROSS_THRESHOLD_POINTS = 1.0

# SL/TP step params (taken from your code comments/vars)
# Long
SL_DISTANCE_POINTS = 1234.0
TRAIL_SL_STEP1_TRIGGER_LONG_POINTS = 300.0
TRAIL_SL_STEP1_NEW_SL_POINTS_LONG = -700.0   # interpreted as: SL = entry + (-700)
TRAIL_SL_STEP2_TRIGGER_LONG_POINTS = 500.0
TRAIL_SL_STEP2_NEW_SL_POINTS_LONG = 460.0    # SL = entry + 460 (breakeven-ish)
TRAIL_SL_STEP3_TRIGGER_LONG_POINTS = 700.0
TRAIL_SL_STEP3_NEW_SL_POINTS_LONG = 650.0    # SL = entry + 650  (TP-like)

# Short
TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS = 300.0
TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT = 700.0   # SL = entry + 700 (above entry)
TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS = 500.0
TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT = -460.0  # SL = entry - 460 (breakeven-ish)
TRAIL_SL_STEP3_TRIGGER_SHORT_POINTS = 700.0
TRAIL_SL_STEP3_NEW_SL_POINTS_SHORT = -650.0  # SL = entry - 650 (TP-like)

# ===================================================


def fetch_ohlcv_2y(exchange, symbol, timeframe='1h', years=2):
    """Fetch approximately `years` of OHLCV with pagination."""
    now = int(time.time() * 1000)
    since_dt = datetime.now(timezone.utc) - timedelta(days=365 * years + 5)
    since = int(since_dt.timestamp() * 1000)

    all_rows = []
    limit = 1500  # binance allows up to 1500 for 1h
    while True:
        o = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
        if not o:
            break
        all_rows.extend(o)
        # advance since by last close + 1ms
        since = o[-1][0] + 1
        # stop if we've reached near present
        if since >= now - 60_000:
            break
        # sleep modestly to respect rate limits
        time.sleep(exchange.rateLimit / 1000.0)

        # safety stop if too many
        if len(all_rows) > 200000:
            break

    df = pd.DataFrame(all_rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df.drop_duplicates(subset='ts', keep='last', inplace=True)
    df.sort_values('ts', inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def backtest(df: pd.DataFrame):
    # Compute EMAs
    df['ema_fast'] = ema(df['close'], EMA_FAST)
    df['ema_slow'] = ema(df['close'], EMA_SLOW)

    # Use bar close for decisions.
    # Position state
    equity = START_EQUITY
    position = None  # dict: side, entry, size_contracts, sl, step, entry_bar_index
    tp_count = 0
    sl_count = 0
    last_exit_bar = -9999

    def contracts_for_equity(eqt, price):
        # Notional = eqt * RISK_FACTOR * LEVERAGE
        notional = eqt * RISK_FACTOR * LEVERAGE
        if notional <= 0:
            return 0.0
        return notional / price

    for i in range(len(df)):
        if i < EMA_SLOW + 5:
            continue

        row = df.iloc[i]
        prev = df.iloc[i - 1]

        price = float(row['close'])
        high = float(row['high'])
        low = float(row['low'])

        ema_fast = float(row['ema_fast'])
        ema_slow = float(row['ema_slow'])
        ema_fast_prev = float(prev['ema_fast'])
        ema_slow_prev = float(prev['ema_slow'])

        # Manage open position first (intra-bar checks using this bar's high/low)
        if position is not None:
            side = position['side']
            entry = position['entry']
            size = position['size']
            step = position['step']
            sl = position['sl']

            # hit SL?
            if side == 'long':
                # SL is below/above entry depending on the step; check low breach
                if low <= sl:
                    # assume fill at sl (no slippage)
                    pnl = (sl - entry) * size
                    equity += pnl
                    sl_count += 1 if step < 2 else 0  # step>=2 treated as TP-like exit
                    tp_count += 1 if step >= 2 else 0
                    position = None
                    last_exit_bar = i
                else:
                    pnl_points = price - entry
                    # Step triggers
                    if step == 0 and pnl_points >= TRAIL_SL_STEP1_TRIGGER_LONG_POINTS:
                        position['step'] = 1
                        position['sl'] = entry + TRAIL_SL_STEP1_NEW_SL_POINTS_LONG
                    elif step == 1 and pnl_points >= TRAIL_SL_STEP2_TRIGGER_LONG_POINTS:
                        position['step'] = 2
                        position['sl'] = entry + TRAIL_SL_STEP2_NEW_SL_POINTS_LONG
                    elif step == 2 and pnl_points >= TRAIL_SL_STEP3_TRIGGER_LONG_POINTS:
                        position['step'] = 3
                        position['sl'] = entry + TRAIL_SL_STEP3_NEW_SL_POINTS_LONG
                        # if price quickly pulls back below the new SL within same bar low:
                        if low <= position['sl']:
                            pnl = (position['sl'] - entry) * size
                            equity += pnl
                            tp_count += 1
                            position = None
                            last_exit_bar = i

            else:  # short
                if high >= sl:
                    pnl = (entry - sl) * size
                    equity += pnl
                    sl_count += 1 if step < 2 else 0
                    tp_count += 1 if step >= 2 else 0
                    position = None
                    last_exit_bar = i
                else:
                    pnl_points = entry - price
                    if step == 0 and pnl_points >= TRAIL_SL_STEP1_TRIGGER_SHORT_POINTS:
                        position['step'] = 1
                        position['sl'] = entry + TRAIL_SL_STEP1_NEW_SL_POINTS_SHORT
                    elif step == 1 and pnl_points >= TRAIL_SL_STEP2_TRIGGER_SHORT_POINTS:
                        position['step'] = 2
                        position['sl'] = entry + TRAIL_SL_STEP2_NEW_SL_POINTS_SHORT
                    elif step == 2 and pnl_points >= TRAIL_SL_STEP3_TRIGGER_SHORT_POINTS:
                        position['step'] = 3
                        position['sl'] = entry + TRAIL_SL_STEP3_NEW_SL_POINTS_SHORT
                        if high >= position['sl']:
                            pnl = (entry - position['sl']) * size
                            equity += pnl
                            tp_count += 1
                            position = None
                            last_exit_bar = i

        # Entry logic (after managing existing pos); allow new entry only if flat
        if position is None and i - last_exit_bar >= MIN_COOLDOWN_BARS:
            crossed_up = (ema_fast_prev <= ema_slow_prev) and (ema_fast > ema_slow + CROSS_THRESHOLD_POINTS)
            crossed_down = (ema_fast_prev >= ema_slow_prev) and (ema_fast < ema_slow - CROSS_THRESHOLD_POINTS)

            if crossed_up:
                size = contracts_for_equity(equity, price)
                if size > 0:
                    position = {
                        'side': 'long',
                        'entry': price,
                        'size': size,
                        'sl': price - SL_DISTANCE_POINTS,
                        'step': 0,
                        'entry_bar': i,
                    }
            elif crossed_down:
                size = contracts_for_equity(equity, price)
                if size > 0:
                    position = {
                        'side': 'short',
                        'entry': price,
                        'size': size,
                        'sl': price + SL_DISTANCE_POINTS,
                        'step': 0,
                        'entry_bar': i,
                    }

    # Close at last price if still open (mark-to-market without TP/SL classification)
    if position is not None:
        last_price = float(df.iloc[-1]['close'])
        if position['side'] == 'long':
            pnl = (last_price - position['entry']) * position['size']
        else:
            pnl = (position['entry'] - last_price) * position['size']
        equity += pnl
        # do not count as TP/SL since not via stop
        position = None

    return equity, tp_count, sl_count, df


def main():
    print("Connecting to Binance Futures via CCXT...")
    exchange = ccxt.binance({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'},
    })

    print("Fetching OHLCV (~2 years, 1h)... this may take a few minutes on first run.")
    df = fetch_ohlcv_2y(exchange, SYMBOL, TIMEFRAME, YEARS)

    if df.empty:
        raise RuntimeError("No OHLCV data fetched.")

    print(f"Got {len(df)} candles from {datetime.utcfromtimestamp(df['ts'].iloc[0]/1000)} "
          f"to {datetime.utcfromtimestamp(df['ts'].iloc[-1]/1000)}")

    equity, tp_count, sl_count, df = backtest(df)

    print("\n=== BACKTEST RESULTS ===")
    print(f"Initial equity : {START_EQUITY:.2f} USDT")
    print(f"Final equity   : {equity:.2f} USDT")
    print(f"TP count       : {tp_count}")
    print(f"SL count       : {sl_count}")
    print("========================")

if __name__ == "__main__":
    main()
