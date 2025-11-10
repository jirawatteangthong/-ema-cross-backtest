# OKX M15 Bot — EMA50/100 Trend + Nadaraya-Watson Envelope
# Single-file version. Edit config section below as needed.
# .env must contain OKX_API_KEY, OKX_SECRET, OKX_PASSWORD, (optional) TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

import os, time, json, logging, math
from dataclasses import dataclass
from datetime import datetime
import requests
import ccxt
from dotenv import load_dotenv

load_dotenv(override=True)

# ========== ENV (from .env) ==========
API_KEY   = os.getenv('OKX_API_KEY', 'YOUR_OKX_API_KEY')
SECRET    = os.getenv('OKX_SECRET', 'YOUR_OKX_SECRET')
PASSWORD  = os.getenv('OKX_PASSWORD', 'YOUR_OKX_PASSPHRASE')

TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID')

# ========== CONFIG (แก้ในนี้ได้เลย) ==========
SYMBOL_ID    = "BTC-USDT-SWAP"   # OKX swap instrument id
TIMEFRAME    = "15m"
LEVERAGE     = 15
MARGIN_MODE  = "isolated"        # 'isolated' or 'cross'

POSITION_MARGIN_FRACTION = 0.80 # ใช้ 50% ของ free USDT เป็น margin ต่อไม้

EMA_FAST = 50
EMA_SLOW = 100

# Nadaraya-Watson Envelope params
NW_BANDWIDTH = 8.0
NW_MULT      = 3.0
NW_LOOKBACK  = 500   # ต้องมีอย่างน้อย win+1 closed bars

# SL / points (fixed distance from entry)
SL_POINTS = 300.0    # เช่น entry 114500 -> SL long = 114200 ; short = entry + SL_POINTS

# Behavior
# เมื่อโดน SL -> sl_lock_active True; ปลดล็อกเมื่อมีแท่งปิด (closed bar) อยู่ใน zone (between lower & upper)
# หลังปลดล็อก ให้หาสัญญาณตาม EMA ปัจจุบันได้เลย (ไม่ต้องรอ cross ใหม่)

LOOP_SECONDS = 3
LOG_FILE = "bot.log"
STATS_FILE = "daily_stats.json"
DAILY_REPORT_HH = 23
DAILY_REPORT_MM = 59

# ========== logging ==========
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")])
log = logging.getLogger("okx.m15.bot")

# ========== Helpers: EMA, Nadaraya ==========
def ema_series(values, period: int):
    n = int(period)
    if not values or len(values) < n:
        return None
    sma = sum(values[:n]) / n
    k = 2.0 / (n + 1)
    out = [None] * (n - 1) + [sma]
    e = sma
    for v in values[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def last_ema(values, period: int):
    es = ema_series(values, period)
    return es[-1] if es else None

def _gauss(x: float, h: float) -> float:
    return math.exp(-(x * x) / (2.0 * h * h))

def nwe_non_repaint(closes: list, h: float, mult: float, win: int):
    """
    closes: list of closed-bar closes (old...latest_closed)
    requires len(closes) >= win+1
    returns upper, lower, mid computed for latest closed bar
    """
    n = len(closes)
    if n < win + 1:
        return None, None, None
    coefs = [_gauss(i, h) for i in range(win)]
    den = sum(coefs)
    s = 0.0
    # endpoint: use closes[-1 - i]
    for i in range(win):
        s += closes[-1 - i] * coefs[i]
    mid = s / den
    # mae on window (exclude the endpoint itself as in original)
    diffs = [abs(closes[-1 - i] - mid) for i in range(1, win + 1)]
    mae = (sum(diffs) / win) * mult
    upper = mid + mae
    lower = mid - mae
    return upper, lower, mid

# ========== Telegram ==========
def tg_send(text: str):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("YOUR") or not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID.startswith("YOUR"):
        # no creds — do nothing (silent)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.get(url, params={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        log.warning(f"TG error: {e}")

# ========== Stats (daily only) ==========
class DailyStats:
    def __init__(self, path=STATS_FILE):
        self.path = path
        self.data = {'date': datetime.now().strftime('%Y-%m-%d'), 'trades': [], 'pnl_usdt': 0.0}
        self._load()
        self._last_report_key = None

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.data.update(loaded)
        except Exception:
            pass

    def _save(self):
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning(f"save stats error: {e}")

    def roll_if_new_day(self):
        today = datetime.now().strftime('%Y-%m-%d')
        if self.data.get('date') != today:
            # send yesterday report then reset
            self.send_report(force=True)
            self.data = {'date': today, 'trades': [], 'pnl_usdt': 0.0}
            self._save()

    def add_trade(self, side, entry, close, qty, pnl_usdt, reason):
        rec = {'time': datetime.now().strftime('%H:%M:%S'),
               'side': side, 'entry': entry, 'close': close, 'qty': qty, 'pnl_usdt': float(pnl_usdt), 'reason': reason}
        self.data['trades'].append(rec)
        self.data['pnl_usdt'] = float(self.data.get('pnl_usdt', 0.0)) + float(pnl_usdt)
        self._save()

    def send_report(self, force=False):
        now = datetime.now()
        key = f"{self.data['date']}:{DAILY_REPORT_HH}:{DAILY_REPORT_MM}"
        if not force:
            if not (now.hour == DAILY_REPORT_HH and now.minute == DAILY_REPORT_MM):
                return
            if self._last_report_key == key:
                return
        total = float(self.data.get('pnl_usdt', 0.0))
        if not self.data['trades'] and total == 0.0 and not force:
            self._last_report_key = key
            return
        lines = [f"📊 <b>สรุปผลรายวัน</b> — {self.data['date']}", f"Σ PnL: <b>{total:+,.2f} USDT</b>"]
        if self.data['trades']:
            lines.append("────────────")
            for t in self.data['trades'][-20:]:
                lines.append(f"{t['time']} | {t['side'].upper()} | {t['entry']:.2f}→{t['close']:.2f} | {t['pnl_usdt']:+.2f} ({t['reason']})")
        tg_send("\n".join(lines))
        self._last_report_key = key

stats = DailyStats()

# ========== Exchange wrapper (OKX) ==========
class OKX:
    def __init__(self):
        self.ex = None
        self.market = None
        self.symbol_u = None
        self.contract_size = 0.01

    def setup(self):
        if not API_KEY or API_KEY.startswith("YOUR") or not SECRET or SECRET.startswith("YOUR") or not PASSWORD or PASSWORD.startswith("YOUR"):
            raise RuntimeError("Set OKX_API_KEY / OKX_SECRET / OKX_PASSWORD in .env")
        self.ex = ccxt.okx({'apiKey': API_KEY, 'secret': SECRET, 'password': PASSWORD, 'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
        self.ex.load_markets()
        self.market = self.ex.market(SYMBOL_ID)
        self.symbol_u = self.market['symbol']
        self.contract_size = float(self.market.get('contractSize') or 0.01)
        try:
            self.ex.set_leverage(LEVERAGE, self.symbol_u, params={'mgnMode': MARGIN_MODE})
        except Exception as e:
            log.warning(f"set_leverage warn: {e}")

    def ticker_last(self):
        return float(self.ex.fetch_ticker(self.symbol_u)['last'])

    def fetch_ohlcv(self, timeframe, limit):
        return self.ex.fetch_ohlcv(self.symbol_u, timeframe=timeframe, limit=limit)

    def free_usdt(self):
        try:
            bal = self.ex.fetch_balance({'type': 'swap'})
        except Exception:
            bal = self.ex.fetch_balance()
        try:
            data = (bal.get('info', {}).get('data') or [])
            if data:
                details = data[0].get('details') or []
                for d in details:
                    if d.get('ccy') == 'USDT':
                        avail = float(d.get('availBal') or 0)
                        frozen = float(d.get('ordFrozen') or 0)
                        return max(0.0, avail - frozen)
        except Exception:
            pass
        v = (bal.get('USDT') or {}).get('free')
        return float(v) if v is not None else 0.0

    def fetch_position(self):
        ps = self.ex.fetch_positions([self.symbol_u])
        for p in ps:
            sym_u = p.get('symbol')
            instId = (p.get('info') or {}).get('instId')
            if (sym_u == self.symbol_u) or (instId == self.market['id']):
                qty = abs(float(p.get('contracts') or 0))
                if qty != 0:
                    return {'side': p.get('side'), 'contracts': qty, 'entry': float(p.get('entryPrice') or 0)}
        return None

    def amount_to_precision(self, qty):
        try: return float(self.ex.amount_to_precision(self.symbol_u, qty))
        except Exception: return float(f"{qty:.4f}")

    def contracts_from_notional(self, price, notional):
        if price <= 0 or self.contract_size <= 0 or notional <= 0:
            return 0.0
        qty = notional / (price * self.contract_size)
        qty = self.amount_to_precision(qty)
        return 0.0 if qty < 0.01 else qty

    def _market(self, side_ccxt, qty, extra=None):
        params = dict(extra or {})
        params.setdefault('tdMode', MARGIN_MODE)
        params.setdefault('posSide', 'long' if side_ccxt.lower()=='buy' else 'short')
        try:
            return self.ex.create_market_order(self.symbol_u, side_ccxt, qty, None, params)
        except Exception:
            return self.ex.create_market_order(self.symbol_u, side_ccxt, qty, None, {'tdMode': MARGIN_MODE})

    def open_market(self, side, notional, price_ref):
        qty = self.contracts_from_notional(price_ref, notional)
        if qty <= 0: return None
        self._market('buy' if side == 'long' else 'sell', qty, {})
        time.sleep(0.6)
        return self.fetch_position()

    def reduce_only_close(self):
        pos = self.fetch_position()
        if not pos: return True
        side = 'sell' if pos['side'] == 'long' else 'buy'
        qty = pos['contracts']
        self._market(side, qty, {'reduceOnly': True})
        time.sleep(0.8)
        for _ in range(10):
            if not self.fetch_position(): break
            time.sleep(0.3)
        return self.fetch_position() is None

# ========== Strategy / Risk ==========
class TrendSide:
    BUY = "buy"
    SELL = "sell"
    NONE = "none"

def trend_from_ema(closes):
    e_fast = last_ema(closes, EMA_FAST)
    e_slow = last_ema(closes, EMA_SLOW)
    if e_fast is None or e_slow is None:
        return TrendSide.NONE
    if e_fast > e_slow: return TrendSide.BUY
    if e_fast < e_slow: return TrendSide.SELL
    return TrendSide.NONE

@dataclass
class Bands:
    upper: float
    lower: float
    mid: float

def compute_bands(closes):
    up, lo, mid = nwe_non_repaint(closes, h=NW_BANDWIDTH, mult=NW_MULT, win=NW_LOOKBACK)
    if up is None: return None
    return Bands(upper=up, lower=lo, mid=mid)

def entry_signal(side_allowed, price_now, bands):
    if side_allowed == TrendSide.BUY and price_now <= bands.lower:
        return "long"
    if side_allowed == TrendSide.SELL and price_now >= bands.upper:
        return "short"
    return None

def tp_hit(pos_side, price_now, bands):
    if pos_side == "long":
        return price_now >= bands.upper
    else:
        return price_now <= bands.lower

@dataclass
class PositionState:
    side: str
    entry: float
    contracts: float
    cs: float
    margin_used: float
    sl_price: float = None

def compute_sl_price(entry_price, side):
    """SL = entry ± SL_POINTS"""
    if side == 'long':
        return entry_price - SL_POINTS
    else:
        return entry_price + SL_POINTS

# ========== Runtime vars ==========
pos_state = None  # PositionState or None
sl_lock_active = False
# price_in_zone flag not strictly needed as we check closed bar in loop; keep for clarity
price_in_zone = False

def open_position(ex, side, price_now):
    global pos_state, sl_lock_active
    free = ex.free_usdt()
    margin = max(0.0, free * POSITION_MARGIN_FRACTION)
    if margin <= 0:
        log.info("No free USDT to open.")
        return False
    notional = margin * LEVERAGE
    pos = ex.open_market(side, notional, price_now)
    if not pos:
        log.info("Open position failed or not confirmed.")
        return False
    # pos: {'side','contracts','entry'}
    ps = PositionState(side=pos['side'], entry=float(pos['entry']), contracts=float(pos['contracts']),
                       cs=float(ex.contract_size), margin_used=float(margin))
    ps.sl_price = compute_sl_price(ps.entry, ps.side)
    # store
    global pos_state
    pos_state = ps
    log.info(f"OPEN {ps.side.upper()} entry={ps.entry:.2f} size={ps.contracts:.4f} margin={ps.margin_used:.2f} SL={ps.sl_price:.2f}")
    return True

def close_position(ex, reason):
    global pos_state, sl_lock_active
    if not pos_state:
        return
    price_now = ex.ticker_last()
    qty = pos_state.contracts
    entry = pos_state.entry
    ex.reduce_only_close()
    # calc pnl
    delta_pts = (price_now - entry) if pos_state.side == 'long' else (entry - price_now)
    pnl = delta_pts * qty * ex.contract_size
    stats.add_trade(pos_state.side, entry, price_now, qty, pnl, reason)
    log.info(f"CLOSE {pos_state.side.upper()} entry={entry:.2f} last={price_now:.2f} qty={qty:.4f} pnl={pnl:+.2f} ({reason})")
    # if closed due to SL -> lock trading until price closes inside zone
    if "SL" in reason.upper():
        sl_lock_active = True
        log.info("SL hit → sl_lock_active = True (pausing all entries until price closes inside envelope zone)")
    pos_state = None

# ========== Main loop ==========
def run():
    global pos_state, sl_lock_active
    ex = OKX()
    ex.setup()
    log.info("Bot started: OKX, M15, EMA50/100 + Nadaraya, Market orders, SL fixed points")
    while True:
        try:
            # daily
            stats.roll_if_new_day()
            stats.send_report(force=False)

            # data: need enough bars for NW_LOOKBACK + 2
            ohlcv = ex.fetch_ohlcv(TIMEFRAME, limit=NW_LOOKBACK + 5)
            if not ohlcv or len(ohlcv) < NW_LOOKBACK + 2:
                time.sleep(LOOP_SECONDS); continue

            # closed bars: exclude the current live bar
            closes_closed = [c[4] for c in ohlcv[:-1]]
            last_closed_close = closes_closed[-1]
            last_price = ex.ticker_last()

            # compute trend and bands
            side_allowed = trend_from_ema(closes_closed)
            bands = compute_bands(closes_closed)
            if bands is None:
                time.sleep(LOOP_SECONDS); continue

            # If we are locked due to SL -> check unlock condition (closed bar close inside zone)
            if sl_lock_active:
                # unlock if last closed close is within [lower, upper]
                if bands.lower <= last_closed_close <= bands.upper:
                    sl_lock_active = False
                    log.info("Price closed inside envelope → unlock trading (sl_lock_active=False). Will follow current EMA trend.")
                    # do not open immediately this same loop; continue to next iteration to use fresh last_price
                    time.sleep(LOOP_SECONDS)
                    continue
                else:
                    # remain locked, do not open entries; but still monitor any existing pos (should be none)
                    time.sleep(LOOP_SECONDS)
                    continue

            # sync position
            live_pos = ex.fetch_position()
            if live_pos is None and pos_state is not None:
                log.info("Position disappeared on exchange; syncing local state.")
                pos_state = None

            # If have position -> manage exits
            if pos_state:
                # refresh pos info
                if live_pos:
                    pos_state.contracts = float(live_pos['contracts'])
                    pos_state.entry = float(live_pos['entry'])
                    pos_state.sl_price = compute_sl_price(pos_state.entry, pos_state.side)
                # TP
                if tp_hit(pos_state.side, last_price, bands):
                    close_position(ex, "TP@Envelope")
                    time.sleep(LOOP_SECONDS); continue
                # SL fixed points
                if pos_state.side == 'long' and last_price <= pos_state.sl_price:
                    close_position(ex, "SL_HIT")
                    time.sleep(LOOP_SECONDS); continue
                if pos_state.side == 'short' and last_price >= pos_state.sl_price:
                    close_position(ex, "SL_HIT")
                    time.sleep(LOOP_SECONDS); continue
                # Trend flip -> close immediately (regardless P/L)
                flip_against = ((pos_state.side == 'long' and side_allowed == TrendSide.SELL) or
                                (pos_state.side == 'short' and side_allowed == TrendSide.BUY))
                if flip_against:
                    close_position(ex, "TrendFlipClose")
                    time.sleep(LOOP_SECONDS); continue
                time.sleep(LOOP_SECONDS); continue

            # No position and not locked -> look for entries (immediate touch)
            if side_allowed in (TrendSide.BUY, TrendSide.SELL):
                sig = entry_signal(side_allowed, last_price, bands)
                if sig:
                    opened = open_position(ex, sig, last_price)
                    if not opened:
                        log.info("Open failed.")
                    time.sleep(LOOP_SECONDS); continue

            time.sleep(LOOP_SECONDS)

        except KeyboardInterrupt:
            log.info("KeyboardInterrupt, exiting.")
            break
        except Exception as e:
            log.exception("Main loop error: %s", e)
            time.sleep(2)

if __name__ == "__main__":
    run()
