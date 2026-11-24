"""Swing Hunter AI backend: scans NSE F&O stocks, stores history in SQLite.
Run: uvicorn main:app --host 0.0.0.0 --port 8000
Data: live F&O list from NSE; daily candles via yfinance (delayed/EOD, unofficial).
For true realtime swap fetch_prices() for Kite/Upstox/Dhan/Angel API.
No data -> no signals. Nothing is ever simulated.
"""
import io, os, sqlite3, threading, datetime as dt
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd, requests, yfinance as yf
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from apscheduler.schedulers.background import BackgroundScheduler

IST = ZoneInfo("Asia/Kolkata")
DB = os.environ.get("DB_PATH", "swing.db")
API_KEY = os.environ.get("API_KEY", "")  # protects POST /api/scan
LOT_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SYMBOL", "NAN"}
lock = threading.Lock()


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_at TEXT, run_type TEXT, data_date TEXT, scanned INTEGER);
        CREATE TABLE IF NOT EXISTS signals(scan_id INTEGER, symbol TEXT, signal TEXT,
          bull INTEGER, bear INTEGER, close REAL, atr REAL, rsi REAL, ema20 REAL,
          ema50 REAL, vol_ratio REAL, up_break INTEGER, down_break INTEGER);
        CREATE INDEX IF NOT EXISTS ix ON signals(scan_id);""")


def universe():
    r = requests.get(LOT_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    s = pd.read_csv(io.StringIO(r.text)).iloc[:, 1].astype(str).str.strip().str.upper()
    return sorted({x for x in s if x not in INDICES and x})


def fetch_prices(symbols):
    out = {}
    for i in range(0, len(symbols), 40):
        chunk = symbols[i:i + 40]
        tk = [f"{s}.NS" for s in chunk]
        d = yf.download(tk, period="9mo", interval="1d", group_by="ticker",
                        auto_adjust=False, threads=True, progress=False)
        for s, t in zip(chunk, tk):
            try:
                df = d[t].dropna()
                if len(df) >= 60:
                    out[s] = df
            except Exception:
                pass
    return out


def analyze(df):
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    e20, e50 = c.ewm(span=20, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    C, H, L = float(c.iloc[-1]), float(h.iloc[-1]), float(l.iloc[-1])
    E20, E50, R = float(e20.iloc[-1]), float(e50.iloc[-1]), float(rsi.iloc[-1])
    vr = float(v.iloc[-1] / v.iloc[-21:-1].mean()) if v.iloc[-21:-1].mean() > 0 else 0
    hh, ll = float(h.iloc[-21:-1].max()), float(l.iloc[-21:-1].min())
    rng = (H - L) or 1e-9
    volpts = max(0, min(20, 20 * (vr - 1) / 0.5))
    bull = (30 if C > E20 > E50 else 15 if C > E50 else 0)
    bull += 20 if 55 <= R <= 72 else 10 if 50 <= R < 55 else 8 if R > 72 else 0
    bull += volpts + (20 if C > hh else 10 if C >= 0.98 * hh else 0) + 10 * (C - L) / rng
    bear = (30 if C < E20 < E50 else 15 if C < E50 else 0)
    bear += 20 if 28 <= R <= 45 else 10 if 45 < R <= 50 else 8 if R < 28 else 0
    bear += volpts + (20 if C < ll else 10 if C <= 1.02 * ll else 0) + 10 * (H - C) / rng
    bull, bear = int(round(bull)), int(round(bear))
    sig = "CE" if bull >= 65 and bull > bear else "PE" if bear >= 65 and bear > bull else "WAIT"
    return dict(signal=sig, bull=bull, bear=bear, close=C, atr=float(atr.iloc[-1]), rsi=R,
                ema20=E20, ema50=E50, vol_ratio=vr, up_break=int(C > hh), down_break=int(C < ll))


def run_scan(run_type="manual"):
    if not lock.acquire(blocking=False):
        return
    try:
        syms = universe()
        prices = fetch_prices(syms)
        if not prices:
            return  # no real data -> store nothing
        newest = max(df.index[-1] for df in prices.values())
        if (dt.datetime.now(IST).date() - newest.date()).days > 5:
            return  # stale data guard
        rows = []
        for s, df in prices.items():
            if df.index[-1] != newest:
                continue  # skip illiquid/suspended symbols with old bars
            rows.append((s, analyze(df)))
        with db() as c:
            sid = c.execute("INSERT INTO scans(run_at,run_type,data_date,scanned) VALUES(?,?,?,?)",
                            (dt.datetime.now(IST).isoformat(timespec="seconds"), run_type,
                             newest.strftime("%Y-%m-%d"), len(rows))).lastrowid
            for s, a in rows:
                c.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (sid, s, a["signal"], a["bull"], a["bear"], a["close"], a["atr"], a["rsi"],
                           a["ema20"], a["ema50"], a["vol_ratio"], a["up_break"], a["down_break"]))
    finally:
        lock.release()


def shape(r, side):
    risk = 1.5 * r["atr"]
    sgn = 1 if side == "bull" else -1
    e = r["close"]
    brk = bool(r["up_break"] if side == "bull" else r["down_break"])
    sig = r["signal"]
    if sig != "WAIT" and (sig == "CE") != (side == "bull"):
        sig = "WAIT"
    return dict(symbol=r["symbol"], signal=sig, score=r[side], rsi=round(r["rsi"], 1),
                ema20=round(r["ema20"], 2), ema50=round(r["ema50"], 2),
                vol_ratio=round(r["vol_ratio"], 2), breakout=("Yes" if brk else "No"),
                close=round(e, 2), entry=round(e, 2), stop_loss=round(e - sgn * risk, 2),
                target1=round(e + sgn * 1.5 * risk, 2), target2=round(e + sgn * 3 * risk, 2))


def payload(sid):
    with db() as c:
        s = c.execute("SELECT * FROM scans WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404, "No scan")
        rows = c.execute("SELECT * FROM signals WHERE scan_id=?", (sid,)).fetchall()
    top = lambda k: [shape(r, k) for r in sorted(rows, key=lambda r: -r[k])[:5]]
    return dict(scan=dict(s), bullish=top("bull"), bearish=top("bear"))


@asynccontextmanager
async def lifespan(app):
    init_db()
    sch = BackgroundScheduler(timezone=IST)
    sch.add_job(run_scan, "cron", day_of_week="mon-fri", hour=9, minute=0, args=["pre_market_9am"])
    sch.add_job(run_scan, "cron", day_of_week="mon-fri", hour=16, minute=0, args=["post_close"])
    sch.start()
    yield
    sch.shutdown()


app = FastAPI(title="Swing Hunter AI", lifespan=lifespan)


@app.get("/api/latest")
def latest():
    with db() as c:
        r = c.execute("SELECT id FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        raise HTTPException(404, "No scan yet")
    return payload(r["id"])


@app.get("/api/history")
def history():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 90")]


@app.get("/api/scan/{sid}")
def scan(sid: int):
    return payload(sid)


@app.post("/api/scan")
def trigger(bg: BackgroundTasks, x_api_key: str = Header("")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "Bad key")
    bg.add_task(run_scan, "manual")
    return {"status": "started"}
