# --- Binance Futures Orderbook Heat -> Telegram (no .env) ---
# แก้ค่า CONFIG ด้านล่างนี้ได้เลย แล้ว push ขึ้น GitHub/Railway เพื่อรัน

import time, math, requests, traceback
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import ccxt

CONFIG = {
    # ====== ตั้งค่าที่นี่ ======
    "EXCHANGE_KIND": "binanceusdm",       # คงไว้ = Futures USDT-M
    "SYMBOL": "BTC/USDT",
    "TIMEFRAME_TAG": "1h",                # ใช้เป็น label ในข้อความ (ไม่ดึงแท่ง)
    "DEPTH_LIMIT": 1000,                  # 5/10/20/50/100/500/1000
    "TOP_N": 5,
    "LOOP_MINUTES": 30,                   # วนลูปทุกกี่นาที
    "TIMEZONE": "Asia/Bangkok",
    "WINDOW_PCT": 0.0,                    # 0 = ปิดฟิลเตอร์ | เช่น 2.0 = โฟกัส ±2% รอบ mid

    # ====== Telegram ======
    # ใส่โทเคน/แชทไอดีไว้ในโค้ดตามที่ต้องการ (ระวังเรื่องความลับใน repo สาธารณะ)
    "TELEGRAM_BOT_TOKEN": "7752789264:AAF-0zdgHsSSYe7PS17ePYThOFP3k7AjxBY",
    "TELEGRAM_CHAT_ID": "8104629569",
}

# --------------------- Utils ---------------------
def now_str(tz_str: str):
    try:
        z = ZoneInfo(tz_str)
    except Exception:
        z = timezone.utc
    return datetime.now(z).strftime("%Y-%m-%d %H:%M:%S %Z")

def send_telegram(token: str, chat_id: str, text: str):
    if not token or not chat_id or "PUT_YOUR" in token or "PUT_YOUR" in chat_id:
        print("[WARN] Telegram not configured; skip send.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, data=payload, timeout=20)
        if r.status_code != 200:
            print(f"[ERROR] Telegram send failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[ERROR] Telegram send exception: {e}")

def fmt_num(n, digits=2):
    try:
        if abs(n) >= 1000:
            return f"{n:,.{digits}f}"
        if abs(n) < 0.01:
            return f"{n:.6f}"
        return f"{n:.{digits}f}"
    except Exception:
        return str(n)

def round_to_step(x: float, step: float, mode=ROUND_DOWN) -> float:
    d = Decimal(str(x))
    s = Decimal(str(step))
    return float(d.quantize(s, rounding=mode))

def detect_tick_and_step(market: dict):
    price_tick = None
    amount_step = None
    info = market.get("info", {})
    filters = info.get("filters", [])
    for f in filters:
        t = f.get("filterType")
        if t == "PRICE_FILTER" and f.get("tickSize") is not None:
            price_tick = float(f.get("tickSize"))
        if t in ("LOT_SIZE", "MARKET_LOT_SIZE") and f.get("stepSize") is not None:
            amount_step = float(f.get("stepSize"))
    if price_tick is None:
        p = market.get("precision", {}).get("price", 1)
        price_tick = 10 ** (-p)
    if amount_step is None:
        a = market.get("precision", {}).get("amount", 3)
        amount_step = 10 ** (-a)
    return price_tick, amount_step

def pick_exchange(kind: str):
    if kind.lower() != "binanceusdm":
        raise RuntimeError("This script is fixed to Binance USDT-M Futures (binanceusdm).")
    return ccxt.binanceusdm({
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })

def compute_top_levels(orderbook: dict, top_n: int, window_pct: float = 0.0):
    bids = orderbook.get("bids", []) or []
    asks = orderbook.get("asks", []) or []
    if not bids or not asks:
        return [], []
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    if window_pct and window_pct > 0:
        lo = mid * (1 - window_pct / 100.0)
        hi = mid * (1 + window_pct / 100.0)
        bids = [b for b in bids if b[0] >= lo]
        asks = [a for a in asks if a[0] <= hi]
    top_bids = sorted(bids, key=lambda x: x[1], reverse=True)[:top_n]
    top_asks = sorted(asks, key=lambda x: x[1], reverse=True)[:top_n]
    return top_bids, top_asks

def snapshot_and_report():
    ex = pick_exchange(CONFIG["EXCHANGE_KIND"])
    ex.load_markets()
    mkt = ex.market(CONFIG["SYMBOL"])
    price_tick, amount_step = detect_tick_and_step(mkt)

    ob = ex.fetch_order_book(CONFIG["SYMBOL"], limit=CONFIG["DEPTH_LIMIT"])
    bids, asks = ob.get("bids", []), ob.get("asks", [])
    if not bids or not asks:
        raise RuntimeError("Empty order book.")
    best_bid, best_ask = bids[0][0], asks[0][0]
    spread = best_ask - best_bid
    mid = (best_ask + best_bid) / 2.0

    top_bids, top_asks = compute_top_levels(ob, CONFIG["TOP_N"], CONFIG["WINDOW_PCT"])

    title = (
        f"📊 Orderbook Heat — {CONFIG['SYMBOL']} @ BINANCE USDT-M\n"
        f"⏱ {CONFIG['TIMEFRAME_TAG']} • {now_str(CONFIG['TIMEZONE'])}"
    )
    header = [
        f"Depth: {CONFIG['DEPTH_LIMIT']} | Mid: {fmt_num(mid, 2)} | "
        f"Spread: {fmt_num(spread, 2)} ({fmt_num(100*spread/mid, 4)}%)",
        f"Window: {'±'+str(CONFIG['WINDOW_PCT'])+'%' if CONFIG['WINDOW_PCT']>0 else 'Full book'} "
        f"• Tick: {price_tick} • Step: {amount_step}",
        ""
    ]

    def side_lines(name, rows):
        lines = [f"— {name} TOP {CONFIG['TOP_N']} —"]
        for i, (px, amt) in enumerate(rows, 1):
            notional = px * amt
            px_r = round_to_step(px, price_tick, ROUND_DOWN)
            amt_r = round_to_step(amt, amount_step, ROUND_DOWN)
            lines.append(f"{i}) {fmt_num(px_r, 2)} — {fmt_num(amt_r, 6)} BTC (≈ {fmt_num(notional, 2)} USDT)")
        return lines

    msg_lines = [title, *header]
    msg_lines += side_lines("Bids (ใหญ่สุด)", top_bids)
    msg_lines.append("")
    msg_lines += side_lines("Asks (ใหญ่สุด)", top_asks)

    text = "\n".join(msg_lines)
    print("\n" + text + "\n")
    send_telegram(CONFIG["TELEGRAM_BOT_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"], text)

def main():
    print(f"[START] {CONFIG['SYMBOL']} on BINANCE USDT-M | TOP_N={CONFIG['TOP_N']} | "
          f"DEPTH_LIMIT={CONFIG['DEPTH_LIMIT']} | LOOP_MINUTES={CONFIG['LOOP_MINUTES']}")
    while True:
        try:
            snapshot_and_report()
        except Exception as e:
            err = f"[ERROR] {type(e).__name__}: {e}"
            print(err)
            traceback.print_exc()
            try:
                send_telegram(CONFIG["TELEGRAM_BOT_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"],
                              f"⚠️ Orderbook reporter error:\n{err}")
            except Exception:
                pass
        finally:
            time.sleep(CONFIG["LOOP_MINUTES"] * 60)

if __name__ == "__main__":
    main()
