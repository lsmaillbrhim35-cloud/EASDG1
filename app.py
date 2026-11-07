"""
MT5 Scalping Bot + Dashboard (ملف واحد)
تشغيل: uvicorn app:app --host 0.0.0.0 --port $PORT
متغيرات البيئة: METAAPI_TOKEN, DASHBOARD_PASSWORD, (اختياري) ALLOW_LIVE=true
"""
import asyncio
import os
import secrets
import time
from datetime import date
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from metaapi_cloud_sdk import MetaApi

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


# ---------------------------------------------------------------- البوت


class Bot:
    def __init__(self):
        self.account = None
        self.conn = None
        self.connected = False
        self.running = False
        self.task = None
        self.mode = "demo"
        self.name = ""
        self.info = {}
        self.positions = []
        self.logs = []
        self.day = None
        self.day_equity = None
        self.cfg = {"symbol": "EURUSD", "volume": 0.01, "max_loss": 3.0}

    def log(self, msg):
        self.logs.append(time.strftime("%H:%M:%S ") + msg)
        self.logs = self.logs[-60:]

    async def connect(self, broker, login, password, server, mode):
        token = os.environ.get("METAAPI_TOKEN")
        if not token:
            raise ValueError("METAAPI_TOKEN غير مضبوط في Render")
        if mode == "live" and os.environ.get("ALLOW_LIVE") != "true":
            raise ValueError("الحساب الحقيقي معطّل. استخدم Demo.")
        await self.stop()
        api = MetaApi(token)
        account = None
        try:
            found = await api.metatrader_account_api.get_accounts_with_infinite_scroll_pagination(
                {"query": str(login)}
            )
            for a in found:
                if str(a.login) == str(login) and a.server == server:
                    account = a
                    break
        except Exception as e:
            self.log(f"بحث الحساب: {e}")
        if account is None:
            account = await api.metatrader_account_api.create_account(
                {
                    "name": f"{broker}-{login}",
                    "type": "cloud-g2",
                    "login": str(login),
                    "password": password,
                    "server": server,
                    "platform": "mt5",
                    "magic": 123456,
                }
            )
        if account.state != "DEPLOYED":
            await account.deploy()
        self.log("بانتظار اتصال الوسيط...")
        await account.wait_connected()
        conn = account.get_rpc_connection()
        await conn.connect()
        await conn.wait_synchronized()
        self.account, self.conn = account, conn
        self.mode, self.name = mode, f"{broker}-{login}"
        self.connected = True
        await self.refresh()
        self.log(f"تم الاتصال ({mode})")

    async def refresh(self):
        if not (self.connected and self.conn):
            return
        try:
            self.info = await self.conn.get_account_information()
            self.positions = await self.conn.get_positions()
        except Exception as e:
            self.log(f"تحديث: {e}")

    async def start(self, cfg):
        if not self.connected:
            raise ValueError("اتصل بحساب MT5 أولاً")
        if self.running:
            return
        self.cfg.update({k: v for k, v in cfg.items() if v is not None})
        self.day = None
        self.running = True
        self.task = asyncio.create_task(self._loop())
        self.log("تم تشغيل البوت")

    async def stop(self):
        was = self.running
        self.running = False
        if self.task:
            self.task.cancel()
            self.task = None
        if was:
            self.log("تم إيقاف البوت")

    async def _loop(self):
        """
        الاستراتيجية (سكالبينغ M1):
        - الاتجاه: السعر فوق/تحت EMA100
        - الدخول: تقاطع EMA9 مع EMA21 بنفس اتجاه EMA100
        - فلتر الزخم: RSI بين 50-70 للشراء، 30-50 للبيع
        - وقف الخسارة 1.5 ATR، الهدف 2.25 ATR (نسبة 1:1.5)
        - فلتر السبريد، صفقة واحدة فقط، حد خسارة يومي
        """
        c = self.cfg
        last_candle = None
        cooldown_until = 0
        while self.running:
            try:
                await self.refresh()
                eq = self.info.get("equity")
                today = date.today()
                if self.day != today and eq is not None:
                    self.day, self.day_equity = today, eq
                if eq is not None and self.day_equity and eq <= self.day_equity * (1 - c["max_loss"] / 100):
                    self.log(f"حد الخسارة اليومي {c['max_loss']}% - تم الإيقاف")
                    self.running = False
                    break

                has_pos = any(p.get("symbol") == c["symbol"] for p in self.positions)
                if not has_pos and time.time() >= cooldown_until:
                    candles = await self.account.get_historical_candles(c["symbol"], "1m", None, 300)
                    candles = sorted(candles, key=lambda x: x["time"])[:-1]  # بدون الشمعة الجارية
                    if len(candles) > 120 and candles[-1]["time"] != last_candle:
                        last_candle = candles[-1]["time"]
                        cl = [x["close"] for x in candles]
                        hi = [x["high"] for x in candles]
                        lo = [x["low"] for x in candles]
                        f, s, t = ema(cl, 9), ema(cl, 21), ema(cl, 100)
                        r = rsi(cl)
                        a = atr(hi, lo, cl)
                        side = None
                        if f[-2] <= s[-2] and f[-1] > s[-1] and cl[-1] > t[-1] and 50 < r < 70:
                            side = "buy"
                        elif f[-2] >= s[-2] and f[-1] < s[-1] and cl[-1] < t[-1] and 30 < r < 50:
                            side = "sell"
                        if side:
                            price = await self.conn.get_symbol_price(c["symbol"])
                            spread = price["ask"] - price["bid"]
                            if spread > 0.3 * a:
                                self.log(f"سبريد عالٍ، تجاهل إشارة {side}")
                            else:
                                try:
                                    spec = await self.conn.get_symbol_specification(c["symbol"])
                                    digits = int(spec.get("digits", 5))
                                except Exception:
                                    digits = 5
                                opts = {"comment": "scalp-bot"}
                                if side == "buy":
                                    p = price["ask"]
                                    sl, tp = round(p - 1.5 * a, digits), round(p + 2.25 * a, digits)
                                    await self.conn.create_market_buy_order(c["symbol"], c["volume"], sl, tp, opts)
                                else:
                                    p = price["bid"]
                                    sl, tp = round(p + 1.5 * a, digits), round(p - 2.25 * a, digits)
                                    await self.conn.create_market_sell_order(c["symbol"], c["volume"], sl, tp, opts)
                                self.log(f"{side.upper()} {c['symbol']} @ {p} SL {sl} TP {tp} RSI {r:.0f}")
                                cooldown_until = time.time() + 60
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
    broker: str
    login: str
    password: str
    server: str
    mode: str = "demo"


class StartReq(BaseModel):
    symbol: Optional[str] = None
    volume: Optional[float] = None
    max_loss: Optional[float] = None


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.post("/connect")
async def connect(r: ConnectReq, x_auth: Optional[str] = Header(None)):
    auth(x_auth)
    try:
        await bot.connect(r.broker, r.login, r.password, r.server, r.mode)
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


@app.get("/status")
async def status(x_auth: Optional[str] = Header(None)):
    auth(x_auth)
    i = bot.info or {}
    return {
        "connected": bot.connected,
        "running": bot.running,
        "mode": bot.mode,
        "account": bot.name,
        "balance": i.get("balance"),
        "equity": i.get("equity"),
        "currency": i.get("currency"),
        "positions": [
            {"symbol": p.get("symbol"), "type": str(p.get("type")), "volume": p.get("volume"), "profit": p.get("profit")}
            for p in bot.positions
        ],
        "logs": bot.logs[-15:],
    }


# ---------------------------------------------------------------- الواجهة

PAGE = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MT5 Bot</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--tx:#e6edf3;--mut:#8b949e;--g:#238636;--r:#da3633}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,sans-serif;padding:16px;max-width:520px;margin-inline:auto}
h1{font-size:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
.card h2{margin:0 0 10px;font-size:15px;color:var(--mut)}
input,select{width:100%;padding:10px;margin-bottom:8px;background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--tx);font-size:16px}
button{width:100%;padding:13px;border:0;border-radius:8px;color:#fff;font-size:16px;font-weight:700;margin-bottom:8px}
.b{background:#1f6feb}.g{background:var(--g)}.r{background:var(--r)}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-inline-end:6px;background:var(--r)}
.on{background:var(--g)}
.row{display:flex;justify-content:space-between;padding:4px 0}
pre{white-space:pre-wrap;font-size:12px;color:var(--mut);margin:0;direction:ltr;text-align:left}
#err{color:#ff7b72;font-size:14px;min-height:18px}
</style>
</head>
<body>
<h1>لوحة بوت MT5</h1>
<div id="err"></div>

<div class="card">
  <h2>حساب MT5</h2>
  <div><span class="dot" id="cdot"></span><span id="ctext">Disconnected</span></div>
  <div id="acc" class="row"></div>
  <div id="bal" class="row"></div>
  <input id="broker" placeholder="اسم الوسيط">
  <input id="login" placeholder="رقم الحساب (Login)" inputmode="numeric">
  <input id="server" placeholder="السيرفر (Server)">
  <input id="password" type="password" placeholder="كلمة مرور MT5">
  <select id="mode"><option value="demo">Demo</option><option value="live">Live</option></select>
  <button class="b" onclick="connect()">اتصال</button>
</div>

<div class="card">
  <h2>BOT CONTROL</h2>
  <div><span class="dot" id="rdot"></span><span id="rtext">Stopped</span></div>
  <input id="symbol" value="EURUSD" placeholder="الرمز">
  <input id="volume" value="0.01" inputmode="decimal" placeholder="الحجم (Lot)">
  <input id="maxloss" value="3" inputmode="decimal" placeholder="حد الخسارة اليومي %">
  <button class="g" onclick="act('/bot/start',{symbol:v('symbol'),volume:parseFloat(v('volume')),max_loss:parseFloat(v('maxloss'))})">START BOT</button>
  <button class="r" onclick="act('/bot/stop')">STOP BOT</button>
</div>

<div class="card"><h2>الصفقات المفتوحة</h2><div id="pos">-</div></div>
<div class="card"><h2>السجل</h2><pre id="log"></pre></div>

<script>
const v=id=>document.getElementById(id).value;
let pw=sessionStorage.getItem('pw')||'';
function ensurePw(){if(!pw){pw=prompt('كلمة مرور اللوحة')||'';sessionStorage.setItem('pw',pw)}}
async function api(path,method='GET',body){
  ensurePw();
  const r=await fetch(path,{method,headers:{'Content-Type':'application/json','x-auth':pw},body:body?JSON.stringify(body):undefined});
  const d=await r.json().catch(()=>({}));
  if(r.status===401){sessionStorage.removeItem('pw');pw='';}
  if(!r.ok)throw new Error(d.detail||'خطأ');
  return d;
}
function err(e){document.getElementById('err').textContent=e?e.message:''}
async function connect(){
  err();
  try{
    document.getElementById('ctext').textContent='جارٍ الاتصال...';
    await api('/connect','POST',{broker:v('broker'),login:v('login'),server:v('server'),password:v('password'),mode:v('mode')});
    document.getElementById('password').value='';
    poll();
  }catch(e){err(e);poll()}
}
async function act(p,b){err();try{await api(p,'POST',b||{});poll()}catch(e){err(e)}}
async function poll(){
  try{
    const s=await api('/status');
    document.getElementById('cdot').className='dot'+(s.connected?' on':'');
    document.getElementById('ctext').textContent=s.connected?'Connected':'Disconnected';
    document.getElementById('rdot').className='dot'+(s.running?' on':'');
    document.getElementById('rtext').textContent=s.running?'Running':'Stopped';
    document.getElementById('acc').textContent=s.connected?s.account+' ('+s.mode+')':'';
    document.getElementById('bal').textContent=s.balance!=null?'الرصيد: '+s.balance+' '+(s.currency||'')+' | Equity: '+s.equity:'';
    document.getElementById('pos').innerHTML=s.positions.length?s.positions.map(p=>'<div class="row"><span>'+p.symbol+' '+p.type.replace('POSITION_TYPE_','')+' '+p.volume+'</span><span>'+p.profit+'</span></div>').join(''):'لا توجد صفقات';
    document.getElementById('log').textContent=s.logs.join('\n');
  }catch(e){}
}
setInterval(poll,4000);poll();
</script>
</body>
</html>
"""
