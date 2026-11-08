"""
Capital.com Scalping Bot + Dashboard (ملف واحد - بدون أي مكتبات خارجية عدا fastapi و uvicorn)
تشغيل: uvicorn app:app --host 0.0.0.0 --port $PORT
متغيرات البيئة المطلوبة: DASHBOARD_PASSWORD
اختيارية: ALLOW_LIVE=true, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import asyncio
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- المؤشرات


def ema(values, period):
    k = 2 / (period + 1)
    out = []
    for i, v in enumerate(values):
        out.append(v if i == 0 else v * k + out[-1] * (1 - k))
    return out


def rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return sum(trs[-period:]) / period


def _tg_send(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)


# ---------------------------------------------------------------- المحاكاة (Backtest)

YF = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "GOLD": "GC=F",  # عقود الذهب الآجلة (قريبة من سعر الذهب الفوري)
    "BTCUSD": "BTC-USD",
}
YF_RANGE = {"1m": "7d", "5m": "30d"}


def _fetch_yahoo(sym, tf):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(YF[sym])}?interval={tf}&range={YF_RANGE[tf]}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = json.load(urllib.request.urlopen(req, timeout=25))
    res = data["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = []
    for t, o, h, l, c in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"]):
        if None in (o, h, l, c):
            continue
        rows.append((t, o, h, l, c))
    return rows


def rsi_series(closes, period=14):
    n = len(closes)
    out = [50.0] * n
    if n <= period + 1:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains[i] = max(d, 0)
        losses[i] = max(-d, 0)
    ag = sum(gains[1 : period + 1]) / period
    al = sum(losses[1 : period + 1]) / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, n):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr_series(hi, lo, cl, period=14):
    n = len(cl)
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
    out = [0.0] * n
    for i in range(period, n):
        out[i] = sum(tr[i - period + 1 : i + 1]) / period
    return out


def run_backtest(rows, c):
    """نفس منطق البوت. النتائج بوحدة R (R = مقدار المخاطرة في الصفقة = وقف الخسارة)."""
    cl = [x[4] for x in rows]
    hi = [x[2] for x in rows]
    lo = [x[3] for x in rows]
    n = len(rows)
    f, s, t = ema(cl, c["fast"]), ema(cl, c["slow"]), ema(cl, c["trend"])
    r = rsi_series(cl)
    a = atr_series(hi, lo, cl)
    slm, tpm, sp = c["sl_mult"], c["tp_mult"], c["spread"]
    trades = []
    i = max(c["trend"], 20) + 20
    while i < n - 1:
        side = None
        if f[i - 1] <= s[i - 1] and f[i] > s[i] and cl[i] > t[i] and 50 < r[i] < 70:
            side = 1
        elif f[i - 1] >= s[i - 1] and f[i] < s[i] and cl[i] < t[i] and 30 < r[i] < 50:
            side = -1
        if side is None or a[i] <= 0:
            i += 1
            continue
        entry = cl[i]
        sl = entry - side * slm * a[i]
        tp = entry + side * tpm * a[i]
        res = None
        j = i + 1
        while j < n:
            hit_sl = lo[j] <= sl if side == 1 else hi[j] >= sl
            hit_tp = hi[j] >= tp if side == 1 else lo[j] <= tp
            if hit_sl:  # لو تحققا معًا في نفس الشمعة نفترض الخسارة (تحفظًا)
                res = -(slm + sp) / slm
                break
            if hit_tp:
                res = (tpm - sp) / slm
                break
            j += 1
        if res is None:
            break  # صفقة لم تُغلق قبل نهاية البيانات
        trades.append(res)
        i = j + 1
    nt = len(trades)
    total = sum(trades)
    wins = sum(1 for x in trades if x > 0)
    gw = sum(x for x in trades if x > 0)
    gl = -sum(x for x in trades if x < 0)
    cum, run, peak, dd = [], 0.0, 0.0, 0.0
    for x in trades:
        run += x
        cum.append(round(run, 2))
        peak = max(peak, run)
        dd = max(dd, peak - run)
    step = max(1, len(cum) // 200)
    return {
        "candles": n,
        "trades": nt,
        "win_rate": round(100 * wins / nt, 1) if nt else 0,
        "total_r": round(total, 2),
        "avg_r": round(total / nt, 3) if nt else 0,
        "profit_factor": round(gw / gl, 2) if gl > 0 else None,
        "max_dd_r": round(dd, 2),
        "return_pct": round(total * c["risk_pct"], 1),
        "curve": [0] + cum[::step],
    }


# ---------------------------------------------------------------- عميل Capital.com


class CapitalError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"{code} {msg}")
        self.code = code


class CapitalClient:
    def __init__(self, api_key, identifier, password, demo):
        self.base = (
            "https://demo-api-capital.backend-capital.com" if demo else "https://api-capital.backend-capital.com"
        )
        self.api_key = api_key
        self.identifier = identifier
        self.password = password
        self.cst = None
        self.tok = None
        self.account_id = None
        self.lock = threading.Lock()
        self.last = 0.0
        self.last_login = 0.0

    def _raw(self, method, path, body=None, params=None, auth=True):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Content-Type": "application/json", "X-CAP-API-KEY": self.api_key}
        if auth:
            headers["CST"] = self.cst or ""
            headers["X-SECURITY-TOKEN"] = self.tok or ""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        wait = 0.12 - (time.time() - self.last)  # الحد: 10 طلبات في الثانية
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                self.last = time.time()
                txt = r.read().decode() or "{}"
                return json.loads(txt), r.headers
        except urllib.error.HTTPError as e:
            self.last = time.time()
            raise CapitalError(e.code, e.read().decode()[:300])

    def _login(self):
        wait = 1.1 - (time.time() - self.last_login)  # الحد: طلب جلسة واحد في الثانية
        if wait > 0:
            time.sleep(wait)
        data, h = self._raw(
            "POST",
            "/api/v1/session",
            {"identifier": self.identifier, "password": self.password, "encryptedPassword": False},
            auth=False,
        )
        self.last_login = time.time()
        self.cst = h.get("CST")
        self.tok = h.get("X-SECURITY-TOKEN")
        if not self.cst or not self.tok:
            raise CapitalError(0, "لم يتم استلام رموز الجلسة")
        self.account_id = data.get("currentAccountId")
        return data

    def login(self):
        with self.lock:
            return self._login()

    def req(self, method, path, body=None, params=None):
        with self.lock:
            if not self.cst:
                self._login()
            try:
                return self._raw(method, path, body, params)[0]
            except CapitalError as e:
                if e.code == 401:  # الجلسة تنتهي بعد 10 دقائق خمول
                    self._login()
                    return self._raw(method, path, body, params)[0]
                raise

    def account(self):
        d = self.req("GET", "/api/v1/accounts")
        accs = d.get("accounts", [])
        acc = next((a for a in accs if a.get("accountId") == self.account_id), None)
        if acc is None:
            acc = next((a for a in accs if a.get("preferred")), accs[0] if accs else None)
        if not acc:
            return {}
        b = acc.get("balance", {})
        return {
            "balance": b.get("deposit"),
            "equity": b.get("balance"),
            "profit": b.get("profitLoss"),
            "currency": acc.get("currency"),
        }

    def positions(self):
        d = self.req("GET", "/api/v1/positions")
        out = []
        for it in d.get("positions", []):
            p, m = it.get("position", {}), it.get("market", {})
            out.append(
                {
                    "id": p.get("dealId"),
                    "symbol": m.get("epic"),
                    "type": p.get("direction"),
                    "volume": p.get("size"),
                    "profit": p.get("upl"),
                }
            )
        return out

    def market(self, epic):
        d = self.req("GET", f"/api/v1/markets/{urllib.parse.quote(epic)}")
        s, r = d.get("snapshot", {}), d.get("dealingRules", {})

        def g(k):
            return (r.get(k) or {}).get("value")

        return {
            "status": s.get("marketStatus"),
            "bid": s.get("bid"),
            "offer": s.get("offer"),
            "digits": int(s.get("decimalPlacesFactor", 5)),
            "min": g("minDealSize"),
            "max": g("maxDealSize"),
            "inc": g("minSizeIncrement"),
            "min_dist": r.get("minStopOrProfitDistance"),
        }

    def candles(self, epic, resolution, n):
        d = self.req("GET", f"/api/v1/prices/{urllib.parse.quote(epic)}", params={"resolution": resolution, "max": n})
        rows = []
        for p in d.get("prices", []):
            def mid(k):
                x = p[k]
                return (x["bid"] + x["ask"]) / 2

            rows.append(
                {
                    "time": p.get("snapshotTimeUTC") or p.get("snapshotTime"),
                    "high": mid("highPrice"),
                    "low": mid("lowPrice"),
                    "close": mid("closePrice"),
                }
            )
        rows.sort(key=lambda x: x["time"])
        return rows

    def open(self, epic, direction, size, sl, tp):
        body = {"epic": epic, "direction": direction, "size": size, "stopLevel": sl, "profitLevel": tp}
        return self.req("POST", "/api/v1/positions", body).get("dealReference")

    def confirm(self, ref):
        return self.req("GET", f"/api/v1/confirms/{ref}")

    def close(self, deal_id):
        return self.req("DELETE", f"/api/v1/positions/{deal_id}")

    def transactions(self, days=3):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        f = "%Y-%m-%dT%H:%M:%S"
        d = self.req(
            "GET",
            "/api/v1/history/transactions",
            params={"from": start.strftime(f), "to": end.strftime(f), "type": "TRADE"},
        )
        return d.get("transactions", [])

    def quotes(self, epics):
        d = self.req("GET", "/api/v1/markets", params={"epics": ",".join(epics)})
        out = []
        for m in d.get("markets", []):
            out.append(
                {
                    "symbol": m.get("epic"),
                    "bid": m.get("bid"),
                    "offer": m.get("offer"),
                    "change": m.get("percentageChange"),
                    "status": m.get("marketStatus"),
                    "time": str(m.get("updateTimeUTC") or "")[11:19],
                }
            )
        return out


# ---------------------------------------------------------------- البوت


class Bot:
    def __init__(self):
        self.cl = None
        self.connected = False
        self.running = False
        self.task = None
        self.mode = "demo"
        self.name = ""
        self.info = {}
        self.positions = []
        self.logs = []
        self.curve = []
        self.last_refresh = 0
        self.day = None
        self.day_equity = None
        self.cfg = {
            "symbols": ["EURUSD"],
            "size": None,  # None = الحد الأدنى لكل رمز تلقائيًا
            "max_loss": 3.0,
            "fast": 9,
            "slow": 21,
            "trend": 100,
            "sl_mult": 1.5,
            "tp_mult": 2.25,
        }

    def log(self, msg):
        self.logs.append(time.strftime("%H:%M:%S ") + msg)
        self.logs = self.logs[-60:]

    async def notify(self, msg):
        self.log(msg)
        try:
            await asyncio.to_thread(_tg_send, msg)
        except Exception as e:
            self.log(f"تيليجرام: {e}")

    async def connect(self, identifier, api_key, password, mode):
        if mode == "live" and os.environ.get("ALLOW_LIVE") != "true":
            raise ValueError("الحساب الحقيقي معطّل. استخدم Demo.")
        await self.stop()
        cl = CapitalClient(api_key, identifier, password, demo=(mode != "live"))
        try:
            await asyncio.to_thread(cl.login)
        except CapitalError as e:
            raise ValueError(
                f"فشل تسجيل الدخول ({e}). تأكد من البريد ومفتاح API وكلمة مرور المفتاح، ومن اختيار Demo/Live الصحيح."
            )
        self.cl, self.mode, self.name = cl, mode, identifier
        self.connected = True
        self.curve = []
        await self.refresh(force=True)
        self.log(f"تم الاتصال ({mode})")

    async def refresh(self, force=False):
        if not (self.connected and self.cl):
            return
        if not force and time.time() - self.last_refresh < 4:
            return
        self.last_refresh = time.time()
        try:
            self.info = await asyncio.to_thread(self.cl.account)
            self.positions = await asyncio.to_thread(self.cl.positions)
            eq = self.info.get("equity")
            if eq is not None and (not self.curve or time.time() - self.curve[-1][0] >= 20):
                self.curve.append([time.time(), eq])
                self.curve = self.curve[-500:]
        except Exception as e:
            self.log(f"تحديث: {e}")

    async def history(self):
        if not (self.connected and self.cl):
            return []
        tx = await asyncio.to_thread(self.cl.transactions, 3)
        out = []
        for t in tx:
            if "closed" not in str(t.get("note", "")).lower():
                continue
            try:
                profit = float(t.get("size"))
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "time": str(t.get("dateUtc") or t.get("date"))[:16].replace("T", " "),
                    "symbol": t.get("instrumentName"),
                    "type": "",
                    "profit": round(profit, 2),
                }
            )
        out.sort(key=lambda x: x["time"], reverse=True)
        return out[:30]

    async def close_all(self):
        if not self.connected:
            raise ValueError("غير متصل")
        await self.refresh(force=True)
        n = 0
        for p in list(self.positions):
            try:
                await asyncio.to_thread(self.cl.close, p["id"])
                n += 1
            except Exception as e:
                self.log(f"فشل إغلاق {p.get('symbol')}: {e}")
        await self.notify(f"تم إغلاق {n} صفقة")
        await self.refresh(force=True)

    async def start(self, req):
        if not self.connected:
            raise ValueError("اتصل بحساب Capital.com أولاً")
        if self.running:
            return
        c = dict(self.cfg)
        if req.get("symbols"):
            syms = [s.strip() for s in req["symbols"].split(",") if s.strip()]
            if not syms or len(syms) > 5:
                raise ValueError("اكتب من 1 إلى 5 رموز مفصولة بفاصلة")
            c["symbols"] = syms
        c["size"] = req.get("size")
        for k in ("max_loss", "fast", "slow", "trend", "sl_mult", "tp_mult"):
            if req.get(k) is not None:
                c[k] = req[k]
        if not (2 <= c["fast"] < c["slow"] < c["trend"] <= 200):
            raise ValueError("يجب أن يكون: السريع < البطيء < الاتجاه، والاتجاه ≤ 200")
        if (c["size"] is not None and c["size"] <= 0) or not (0 < c["max_loss"] <= 50):
            raise ValueError("قيم غير صحيحة (الحجم/حد الخسارة)")
        if c["sl_mult"] <= 0 or c["tp_mult"] <= 0:
            raise ValueError("قيم غير صحيحة (SL/TP)")
        for sym in c["symbols"]:
            try:
                m = await asyncio.to_thread(self.cl.market, sym)
            except CapitalError as e:
                raise ValueError(f"الرمز {sym} غير صالح في Capital.com ({e})")
            if c["size"] is not None and m["min"] is not None and c["size"] < m["min"]:
                raise ValueError(f"الحد الأدنى للحجم في {sym} هو {m['min']}")
            if c["size"] is not None and m["max"] and c["size"] > m["max"]:
                raise ValueError(f"الحد الأقصى للحجم في {sym} هو {m['max']}")
            self.log(f"{sym}: الحد الأدنى للحجم {m['min']} | السوق {m['status']}")
        self.cfg = c
        self.day = None
        self.running = True
        self.task = asyncio.create_task(self._loop())
        await self.notify(f"تم تشغيل البوت على: {', '.join(c['symbols'])}")

    async def stop(self):
        was = self.running
        self.running = False
        if self.task:
            self.task.cancel()
            self.task = None
        if was:
            await self.notify("تم إيقاف البوت")

    def _size(self, m):
        s = self.cfg["size"] if self.cfg["size"] is not None else m["min"]
        if s is None:
            s = 1
        inc = m.get("inc")
        if inc:
            s = round(round(s / inc) * inc, 8)
        if m.get("min") is not None and s < m["min"]:
            s = m["min"]
        return s

    @staticmethod
    def _min_dist(m, price):
        r = m.get("min_dist") or {}
        v, u = r.get("value"), r.get("unit")
        if v is None:
            return 0
        return price * v / 100 if u == "PERCENTAGE" else v

    async def _scan(self, sym, state):
        """
        الاستراتيجية (سكالبينغ M1):
        - الاتجاه: السعر فوق/تحت EMA الاتجاه
        - الدخول: تقاطع EMA السريع مع البطيء بنفس الاتجاه
        - فلتر الزخم: RSI بين 50-70 للشراء، 30-50 للبيع
        - وقف الخسارة والهدف حسب ATR، فلتر سبريد، صفقة واحدة لكل رمز
        """
        c = self.cfg
        if any(p.get("symbol") == sym for p in self.positions):
            return
        if time.time() < state["cool"].get(sym, 0):
            return
        need = min(1000, max(300, c["trend"] * 3))
        candles = await asyncio.to_thread(self.cl.candles, sym, "MINUTE", need)
        candles = candles[:-1]  # بدون الشمعة الجارية
        if len(candles) <= c["trend"] + 20 or candles[-1]["time"] == state["last"].get(sym):
            return
        state["last"][sym] = candles[-1]["time"]
        closes = [x["close"] for x in candles]
        hi = [x["high"] for x in candles]
        lo = [x["low"] for x in candles]
        f, s, t = ema(closes, c["fast"]), ema(closes, c["slow"]), ema(closes, c["trend"])
        r = rsi(closes)
        a = atr(hi, lo, closes)
        side = None
        if f[-2] <= s[-2] and f[-1] > s[-1] and closes[-1] > t[-1] and 50 < r < 70:
            side = "buy"
        elif f[-2] >= s[-2] and f[-1] < s[-1] and closes[-1] < t[-1] and 30 < r < 50:
            side = "sell"
        if not side or a <= 0:
            return
        m = await asyncio.to_thread(self.cl.market, sym)
        if m["status"] != "TRADEABLE":
            self.log(f"{sym}: السوق مغلق، تجاهل إشارة {side}")
            return
        if m["offer"] - m["bid"] > 0.3 * a:
            self.log(f"سبريد عالٍ، تجاهل إشارة {side} على {sym}")
            return
        price = m["offer"] if side == "buy" else m["bid"]
        sl_d, tp_d = c["sl_mult"] * a, c["tp_mult"] * a
        md = self._min_dist(m, price)
        if md:  # احترام الحد الأدنى لمسافة الوقف/الهدف مع الحفاظ على النسبة بينهما
            k = max(1.0, md * 1.2 / min(sl_d, tp_d))
            sl_d, tp_d = sl_d * k, tp_d * k
        d = m["digits"]
        if side == "buy":
            direction = "BUY"
            sl, tp = round(price - sl_d, d), round(price + tp_d, d)
        else:
            direction = "SELL"
            sl, tp = round(price + sl_d, d), round(price - tp_d, d)
        size = self._size(m)
        ref = await asyncio.to_thread(self.cl.open, sym, direction, size, sl, tp)
        await asyncio.sleep(0.6)
        conf = await asyncio.to_thread(self.cl.confirm, ref)
        state["cool"][sym] = time.time() + 60
        if conf.get("dealStatus") == "ACCEPTED":
            await self.notify(f"{direction} {sym} size {size} @ {price} SL {sl} TP {tp} RSI {r:.0f}")
        else:
            await self.notify(f"رُفضت الصفقة {sym}: {conf.get('reason') or conf.get('dealStatus')}")

    async def _loop(self):
        state = {"last": {}, "cool": {}}
        while self.running:
            try:
                await self.refresh(force=True)
                eq = self.info.get("equity")
                today = date.today()
                if self.day != today and eq is not None:
                    self.day, self.day_equity = today, eq
                if eq is not None and self.day_equity and eq <= self.day_equity * (1 - self.cfg["max_loss"] / 100):
                    self.running = False
                    await self.notify(f"حد الخسارة اليومي {self.cfg['max_loss']}% - تم الإيقاف")
                    break
                for sym in self.cfg["symbols"]:
                    try:
                        await self._scan(sym, state)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self.log(f"{sym}: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"خطأ: {e}")
            await asyncio.sleep(10)


# ---------------------------------------------------------------- الخادم

app = FastAPI()
bot = Bot()


def auth(token):
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if not pw or not token or not secrets.compare_digest(token, pw):
        raise HTTPException(401, "كلمة مرور اللوحة غير صحيحة")


class ConnectReq(BaseModel):
    identifier: str
    api_key: str
    password: str
    mode: str = "demo"


class StartReq(BaseModel):
    symbols: Optional[str] = None
    size: Optional[float] = None
    max_loss: Optional[float] = None
    fast: Optional[int] = None
    slow: Optional[int] = None
    trend: Optional[int] = None
    sl_mult: Optional[float] = None
    tp_mult: Optional[float] = None


class BacktestReq(BaseModel):
    symbol: str = "EURUSD"
    tf: str = "1m"
    fast: Optional[int] = None
    slow: Optional[int] = None
    trend: Optional[int] = None
    sl_mult: Optional[float] = None
    tp_mult: Optional[float] = None
    spread: Optional[float] = None
    risk_pct: Optional[float] = None


@app.post("/backtest")
async def backtest(r: BacktestReq, x_auth: Optional[str] = Header(None)):
    auth(x_auth)
    if r.symbol not in YF:
        raise HTTPException(400, "رمز غير مدعوم في المحاكاة")
    if r.tf not in YF_RANGE:
        raise HTTPException(400, "فريم غير مدعوم")
    c = {
        "fast": r.fast or 9,
        "slow": r.slow or 21,
        "trend": r.trend or 100,
        "sl_mult": r.sl_mult or 1.5,
        "tp_mult": r.tp_mult or 2.25,
        "spread": 0.1 if r.spread is None else r.spread,
        "risk_pct": r.risk_pct or 1.0,
    }
    if not (2 <= c["fast"] < c["slow"] < c["trend"] <= 200):
        raise HTTPException(400, "يجب أن يكون: السريع < البطيء < الاتجاه، والاتجاه ≤ 200")
    if c["sl_mult"] <= 0 or c["tp_mult"] <= 0 or c["spread"] < 0 or not (0 < c["risk_pct"] <= 10):
        raise HTTPException(400, "قيم غير صحيحة")
    try:
        rows = await asyncio.to_thread(_fetch_yahoo, r.symbol, r.tf)
    except Exception as e:
        raise HTTPException(400, f"تعذّر جلب البيانات: {e}")
    if len(rows) < c["trend"] + 100:
        raise HTTPException(400, "البيانات المتاحة قليلة")
    return await asyncio.to_thread(run_backtest, rows, c)


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.post("/connect")
async def connect(r: ConnectReq, x_auth: Optional[str] = Header(None)):
    auth(x_auth)
    try:
        await bot.connect(r.identifier.strip(), r.api_key.strip(), r.password, r.mode)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/bot/start")
async def start(r: StartReq, x_auth: Optional[str] = Header(None)):
    auth(x_auth)
    try:
        await bot.start(r.model_dump())
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/bot/stop")
async def stop(x_auth: Optional[str] = Header(None)):
    auth(x_auth)
    await bot.stop()
    return {"ok": True}


@app.post("/bot/close_all")
async def close_all(x_auth: Optional[str] = Header(None)):
    auth(x_auth)
    try:
        await bot.close_all()
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/history")
async def history(x_auth: Optional[str] = Header(None)):
    auth(x_auth)
    try:
        return {"deals": await bot.history()}
    except Exception as e:
        bot.log(f"سجل الصفقات: {e}")
        return {"deals": []}


_qcache = {"t": 0.0, "key": "", "data": []}


@app.get("/prices")
async def prices(symbols: str = "GOLD,EURUSD,GBPUSD,USDJPY,BTCUSD", x_auth: Optional[str] = Header(None)):
    auth(x_auth)
    if not bot.connected:
        raise HTTPException(400, "اتصل بحساب Capital.com أولاً")
    syms = [s.strip() for s in symbols.split(",") if s.strip()][:20]
    if not syms:
        raise HTTPException(400, "اكتب رمزًا واحدًا على الأقل")
    key = ",".join(syms)
    if _qcache["key"] == key and time.time() - _qcache["t"] < 1.5:
        return {"quotes": _qcache["data"]}
    try:
        data = await asyncio.to_thread(bot.cl.quotes, syms)
    except Exception as e:
        raise HTTPException(400, str(e))
    _qcache.update({"t": time.time(), "key": key, "data": data})
    return {"quotes": data}


@app.get("/status")
async def status(x_auth: Optional[str] = Header(None)):
    auth(x_auth)
    await bot.refresh()
    i = bot.info or {}
    cfg = dict(bot.cfg)
    cfg["symbols"] = ",".join(cfg["symbols"])
    return {
        "connected": bot.connected,
        "running": bot.running,
        "mode": bot.mode,
        "account": bot.name,
        "balance": i.get("balance"),
        "equity": i.get("equity"),
        "profit": i.get("profit"),
        "currency": i.get("currency"),
        "positions": [
            {"symbol": p.get("symbol"), "type": str(p.get("type")), "volume": p.get("volume"), "profit": p.get("profit")}
            for p in bot.positions
        ],
        "curve": [round(x[1], 2) for x in bot.curve[-200:]],
        "logs": bot.logs[-15:],
        "config": cfg,
    }


# ---------------------------------------------------------------- الواجهة

PAGE = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Capital Bot</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--tx:#e6edf3;--mut:#8b949e;--g:#238636;--r:#da3633;--o:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,sans-serif;padding:16px;max-width:520px;margin-inline:auto}
h1{font-size:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
.card h2{margin:0 0 10px;font-size:15px;color:var(--mut)}
label{display:block;font-size:12px;color:var(--mut);margin-bottom:3px}
input,select{width:100%;padding:10px;margin-bottom:8px;background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--tx);font-size:16px}
button{width:100%;padding:13px;border:0;border-radius:8px;color:#fff;font-size:16px;font-weight:700;margin-bottom:8px}
.b{background:#1f6feb}.g{background:var(--g)}.r{background:var(--r)}.o{background:var(--o)}.n{background:#30363d}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-inline-end:6px;background:var(--r)}
.on{background:var(--g)}
.row{display:flex;justify-content:space-between;padding:4px 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0 10px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.chip{width:auto;padding:6px 12px;font-size:13px;background:#21262d;border:1px solid var(--line);border-radius:16px;margin:0;font-weight:600}
pre{white-space:pre-wrap;font-size:12px;color:var(--mut);margin:0;direction:ltr;text-align:left}
canvas{width:100%;height:120px;display:block}
.pos{color:#3fb950}.neg{color:#f85149}
.q{display:grid;grid-template-columns:1.2fr 1fr 1fr .9fr .8fr;gap:4px;padding:6px 0;border-bottom:1px solid var(--line);font-size:14px;direction:ltr;text-align:left}
.qh{color:var(--mut);font-size:12px}
.q small{color:var(--mut)}
#err{color:#ff7b72;font-size:14px;min-height:18px}
</style>
</head>
<body>
<h1>لوحة بوت Capital.com</h1>
<div id="err"></div>

<div class="card">
  <h2>حساب Capital.com</h2>
  <div><span class="dot" id="cdot"></span><span id="ctext">Disconnected</span></div>
  <div id="acc" class="row"></div>
  <div id="bal" class="row"></div>
  <input id="email" type="email" placeholder="البريد الإلكتروني لحسابك" autocomplete="off">
  <input id="apikey" placeholder="مفتاح API (API key)" autocomplete="off">
  <input id="apipass" type="password" placeholder="كلمة مرور المفتاح (Custom password)" autocomplete="off">
  <select id="mode"><option value="demo">Demo</option><option value="live">Live</option></select>
  <button class="b" onclick="connect()">اتصال</button>
</div>

<div class="card">
  <h2>الأسعار اللحظية (تتحدث كل ثانيتين)</h2>
  <input id="watch" value="GOLD,EURUSD,GBPUSD,USDJPY,BTCUSD" placeholder="رموز المراقبة مفصولة بفاصلة">
  <div class="q qh"><span>الرمز</span><span>Bid</span><span>Ask</span><span>التغير</span><span>الوقت</span></div>
  <div id="quotes">اتصل بالحساب لعرض الأسعار</div>
</div>

<div class="card">
  <h2>BOT CONTROL</h2>
  <div><span class="dot" id="rdot"></span><span id="rtext">Stopped</span></div>
  <label>الرموز (حتى 5، مفصولة بفاصلة)</label>
  <input id="symbols" value="EURUSD">
  <div class="chips">
    <button class="chip" onclick="addSym('GOLD')">ذهب GOLD</button>
    <button class="chip" onclick="addSym('EURUSD')">EURUSD</button>
    <button class="chip" onclick="addSym('GBPUSD')">GBPUSD</button>
    <button class="chip" onclick="addSym('USDJPY')">USDJPY</button>
    <button class="chip" onclick="addSym('BTCUSD')">BTCUSD</button>
  </div>
  <div class="grid">
    <div><label>الحجم (فارغ = الحد الأدنى تلقائيًا)</label><input id="volume" value="" inputmode="decimal"></div>
    <div><label>حد الخسارة اليومي %</label><input id="maxloss" value="3" inputmode="decimal"></div>
    <div><label>EMA السريع</label><input id="fast" value="9" inputmode="numeric"></div>
    <div><label>EMA البطيء</label><input id="slow" value="21" inputmode="numeric"></div>
    <div><label>EMA الاتجاه (≤200)</label><input id="trend" value="100" inputmode="numeric"></div>
    <div><label>وقف الخسارة (× ATR)</label><input id="sl" value="1.5" inputmode="decimal"></div>
    <div><label>الهدف (× ATR)</label><input id="tp" value="2.25" inputmode="decimal"></div>
  </div>
  <button class="g" onclick="act('/bot/start',startBody())">START BOT</button>
  <button class="r" onclick="act('/bot/stop')">STOP BOT</button>
  <button class="o" onclick="closeAll()">إغلاق كل الصفقات</button>
  <button class="n" onclick="askNotif()">تفعيل إشعارات المتصفح</button>
</div>

<div class="card">
  <h2>محاكاة الاستراتيجية (Backtest) - بدون حساب أو تكلفة</h2>
  <div class="grid">
    <div><label>الرمز</label><select id="btsym"><option>EURUSD</option><option>GBPUSD</option><option>USDJPY</option><option value="GOLD">GOLD (ذهب)</option><option>BTCUSD</option></select></div>
    <div><label>الفترة</label><select id="bttf"><option value="1m">فريم 1 دقيقة - آخر 7 أيام</option><option value="5m">فريم 5 دقائق - آخر 30 يومًا</option></select></div>
    <div><label>تكلفة السبريد (× ATR)</label><input id="btspread" value="0.1" inputmode="decimal"></div>
    <div><label>المخاطرة لكل صفقة %</label><input id="btrisk" value="1" inputmode="decimal"></div>
  </div>
  <button class="b" onclick="runBacktest()">تشغيل المحاكاة (بإعدادات الاستراتيجية أعلاه)</button>
  <div id="bt"></div>
  <canvas id="chart2"></canvas>
</div>

<div class="card"><h2>منحنى الرصيد (Equity)</h2><canvas id="chart"></canvas></div>
<div class="card"><h2>الصفقات المفتوحة</h2><div id="pos">-</div></div>
<div class="card"><h2>الصفقات المغلقة (آخر 3 أيام)</h2><div id="hist">-</div></div>
<div class="card"><h2>السجل</h2><pre id="log"></pre></div>

<script>
const v=id=>document.getElementById(id).value;
let pw=localStorage.getItem('pw')||'';
let locked=false,inited=false,tick=0,seen=null;
function ensurePw(){if(!pw){locked=false;pw=prompt('كلمة مرور اللوحة')||'';if(pw)localStorage.setItem('pw',pw)}}
async function api(path,method='GET',body){
  ensurePw();
  const r=await fetch(path,{method,headers:{'Content-Type':'application/json','x-auth':pw},body:body?JSON.stringify(body):undefined});
  const d=await r.json().catch(()=>({}));
  if(r.status===401){localStorage.removeItem('pw');pw='';locked=true;}
  if(!r.ok)throw new Error(d.detail||'خطأ');
  return d;
}
function err(e){document.getElementById('err').textContent=e?e.message:''}
function startBody(){return {symbols:v('symbols'),size:parseFloat(v('volume')),max_loss:parseFloat(v('maxloss')),fast:parseInt(v('fast')),slow:parseInt(v('slow')),trend:parseInt(v('trend')),sl_mult:parseFloat(v('sl')),tp_mult:parseFloat(v('tp'))}}
function addSym(s){const el=document.getElementById('symbols');const a=el.value.split(',').map(x=>x.trim()).filter(Boolean);const i=a.indexOf(s);if(i>=0)a.splice(i,1);else a.push(s);el.value=a.join(',')}
function closeAll(){if(confirm('إغلاق كل الصفقات المفتوحة؟'))act('/bot/close_all')}
function askNotif(){if(window.Notification){Notification.requestPermission().then(p=>alert(p==='granted'?'تم تفعيل الإشعارات (تعمل والصفحة مفتوحة)':'لم يتم السماح'))}else alert('المتصفح لا يدعم الإشعارات')}
async function connect(){
  err();
  try{
    document.getElementById('ctext').textContent='جارٍ الاتصال...';
    await api('/connect','POST',{identifier:v('email'),api_key:v('apikey'),password:v('apipass'),mode:v('mode')});
    document.getElementById('apikey').value='';
    document.getElementById('apipass').value='';
    poll();
  }catch(e){err(e);poll()}
}
async function act(p,b){err();try{await api(p,'POST',b||{});poll()}catch(e){err(e)}}
function notif(lines){
  if(seen===null){seen=new Set(lines);return}
  lines.forEach(l=>{
    if(seen.has(l))return;
    seen.add(l);
    if(/BUY|SELL|إيقاف|حد الخسارة|إغلاق|رُفضت/.test(l)&&window.Notification&&Notification.permission==='granted'){try{new Notification('Capital Bot',{body:l})}catch(e){}}
  });
}
function draw(vals,id){
  const c=document.getElementById(id||'chart');
  c.width=c.clientWidth;c.height=120;
  const x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);
  if(!vals||vals.length<2){x.fillStyle='#8b949e';x.font='13px sans-serif';x.fillText('لا توجد بيانات بعد',10,60);return}
  const mn=Math.min(...vals),mx=Math.max(...vals),rg=(mx-mn)||1;
  x.strokeStyle=vals[vals.length-1]>=vals[0]?'#3fb950':'#f85149';x.lineWidth=2;x.beginPath();
  vals.forEach((y,i)=>{const px=i*(c.width-4)/(vals.length-1)+2,py=c.height-8-(y-mn)/rg*(c.height-16);i?x.lineTo(px,py):x.moveTo(px,py)});
  x.stroke();
  x.fillStyle='#8b949e';x.font='11px sans-serif';x.fillText(mx.toFixed(2),4,12);x.fillText(mn.toFixed(2),4,c.height-2);
}
const cls=n=>n>=0?'pos':'neg';
async function runBacktest(){
  err();
  const el=document.getElementById('bt');
  el.textContent='جارٍ الحساب... (قد يستغرق حتى دقيقة)';
  try{
    const r=await api('/backtest','POST',{symbol:v('btsym'),tf:v('bttf'),fast:parseInt(v('fast')),slow:parseInt(v('slow')),trend:parseInt(v('trend')),sl_mult:parseFloat(v('sl')),tp_mult:parseFloat(v('tp')),spread:parseFloat(v('btspread')),risk_pct:parseFloat(v('btrisk'))});
    const row=(a,b,c)=>'<div class="row"><span>'+a+'</span><span class="'+(c||'')+'">'+b+'</span></div>';
    el.innerHTML=row('عدد الشموع',r.candles)+row('عدد الصفقات',r.trades)+row('نسبة الربح',r.win_rate+'%')+row('المجموع (R)',r.total_r,cls(r.total_r))+row('العائد التقريبي',r.return_pct+'%',cls(r.return_pct))+row('معامل الربح',r.profit_factor===null?'-':r.profit_factor)+row('أكبر تراجع (R)',r.max_dd_r)+(r.trades<30?'<div class="neg" style="font-size:13px;padding:6px 0">عدد الصفقات قليل (أقل من 30)، فالنتيجة غير موثوقة.</div>':'');
    draw(r.curve,'chart2');
  }catch(e){el.textContent='';err(e)}
}
async function loadHist(){
  try{
    const h=await api('/history');
    document.getElementById('hist').innerHTML=h.deals.length?h.deals.map(d=>'<div class="row"><span>'+d.time+' '+d.symbol+'</span><span class="'+cls(d.profit)+'">'+d.profit+'</span></div>').join(''):'لا توجد صفقات مغلقة';
  }catch(e){}
}
let lastBid={},qdir={},qbusy=false;
async function loadPrices(){
  if(!window.isConn||qbusy||locked)return;
  qbusy=true;
  try{
    const d=await api('/prices?symbols='+encodeURIComponent(v('watch')));
    document.getElementById('quotes').innerHTML=d.quotes.map(q=>{
      const p=lastBid[q.symbol];
      if(p!==undefined&&q.bid!==p)qdir[q.symbol]=q.bid>p?'pos':'neg';
      lastBid[q.symbol]=q.bid;
      return '<div class="q"><b>'+q.symbol+'</b><span class="'+(qdir[q.symbol]||'')+'">'+q.bid+'</span><span>'+q.offer+'</span><span class="'+cls(q.change||0)+'">'+(q.change==null?'-':q.change+'%')+'</span><small>'+(q.status==='TRADEABLE'?q.time:'مغلق')+'</small></div>';
    }).join('')||'لا توجد بيانات لهذه الرموز';
  }catch(e){document.getElementById('quotes').textContent=e.message}
  qbusy=false;
}
async function poll(){
  if(locked)return;
  try{
    const s=await api('/status');
    if(!inited&&s.config){
      inited=true;const c=s.config;
      document.getElementById('symbols').value=c.symbols;
      document.getElementById('volume').value=(c.size===null||c.size===undefined)?'':c.size;
      document.getElementById('maxloss').value=c.max_loss;
      document.getElementById('fast').value=c.fast;
      document.getElementById('slow').value=c.slow;
      document.getElementById('trend').value=c.trend;
      document.getElementById('sl').value=c.sl_mult;
      document.getElementById('tp').value=c.tp_mult;
    }
    window.isConn=s.connected;
    document.getElementById('cdot').className='dot'+(s.connected?' on':'');
    document.getElementById('ctext').textContent=s.connected?'Connected':'Disconnected';
    document.getElementById('rdot').className='dot'+(s.running?' on':'');
    document.getElementById('rtext').textContent=s.running?'Running':'Stopped';
    document.getElementById('acc').textContent=s.connected?s.account+' ('+s.mode+')':'';
    document.getElementById('bal').textContent=s.equity!=null?'Equity: '+s.equity+' '+(s.currency||'')+' | P/L: '+s.profit:'';
    document.getElementById('pos').innerHTML=s.positions.length?s.positions.map(p=>'<div class="row"><span>'+p.symbol+' '+p.type+' '+p.volume+'</span><span class="'+cls(p.profit)+'">'+p.profit+'</span></div>').join(''):'لا توجد صفقات';
    document.getElementById('log').textContent=s.logs.join('\n');
    draw(s.curve);
    notif(s.logs);
    if(s.connected&&tick%4===0)loadHist();
    tick++;
  }catch(e){}
}
setInterval(poll,4000);poll();
setInterval(loadPrices,2000);
</script>
</body>
</html>
"""
