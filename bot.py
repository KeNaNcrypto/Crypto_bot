# bot.py — Сигнали САМО за РЪСТ (≤5 мин) + важни новини (🟢/🔴)
# Работи с Binance + CoinGecko (LEASH, BONE, TREAT, SNEK).
# НЯМА JobQueue. Използва post_init и application.create_task за фон.

import os, time, asyncio, re, sqlite3
from collections import deque, defaultdict

import requests
import feedparser
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ── ENV ───────────────────────────────────────────────────────────────────────
TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))

DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,ADAUSDT,LEASH,BONE,TREAT,SNEK"
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()]

PRICE_POLL_SECONDS = max(int(os.getenv("PRICE_POLL_SECONDS", "30")), 10)
COOLDOWN_MINUTES   = max(int(os.getenv("COOLDOWN_MINUTES", "10")), 1)
DEFAULT_RISE_PCT   = float(os.getenv("RISE_PCT", "5"))  # по подразбиране 5%
ENABLE_RISE_ALERTS = os.getenv("ENABLE_RISE_ALERTS", "1") == "1"

NEWS_POLL_INTERVAL   = max(int(os.getenv("NEWS_POLL_INTERVAL", "90")), 30)
NEWS_ENABLED_DEFAULT = os.getenv("NEWS_ENABLED", "1") == "1"

FEEDS_DEFAULT = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.binance.com/en/blog/rss",
    "https://www.sec.gov/news/pressreleases.rss",
    "https://www.federalreserve.gov/feeds/press_all.xml",
]

POSITIVE = [
    "approve","approval","approved","etf","listing","list","launch",
    "integration","support","partnership","acquire","acquisition",
    "record","ath","all-time high","buyback","custody","spot etf",
    "rate cut","cut rates","lower rates","reduce rates",
]
NEGATIVE = [
    "hack","exploit","breach","attack","outage","halt","ban",
    "lawsuit","sue","fraud","delay","postpone","suspension","freeze",
    "selloff","liquidation","liquidations","downtime","bankrupt",
    "insolvent","probe","investigation"
]
NEWS_KEYWORDS = [w.strip().lower() for w in os.getenv("NEWS_KEYWORDS","").split(",") if w.strip()] or \
    ["btc","bitcoin","eth","ethereum","sol","solana","sec","federal reserve","fed","inflation",
     "cpi","ppi","rate","rates","etf","listing","approval","hack","exploit","lawsuit","ban","delay","halt",
     "binance","coinbase","grayscale","blackrock","withdrawals","deposits","liquidation","liquidations"]

DEFAULT_CG_IDS = {
    "LEASH": "doge-killer",
    "BONE":  "bone-shibaswap",
    "SNEK":  "snek",
    "TREAT": "treat",
}

# ── Състояние ────────────────────────────────────────────────────────────────
WINDOW_SECONDS = 5 * 60  # ≤5 мин
price_window: dict[str, deque] = {sym: deque() for sym in SYMBOLS}
last_alert_up: dict[str, float] = defaultdict(lambda: 0.0)
current_rise_pct: float = DEFAULT_RISE_PCT

DB_PATH = "news_store.sqlite"

def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    con = db(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS seen(
        feed TEXT, uid TEXT, ts INTEGER, PRIMARY KEY(feed, uid)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS prefs(
        k TEXT PRIMARY KEY, v TEXT
    )""")
    cur.execute("INSERT OR IGNORE INTO prefs(k,v) VALUES('news_enabled', ?)",
                ("1" if NEWS_ENABLED_DEFAULT else "0",))
    con.commit(); con.close()

def get_pref(k: str, default: str = "") -> str:
    con = db(); cur = con.cursor()
    cur.execute("SELECT v FROM prefs WHERE k=?", (k,))
    row = cur.fetchone(); con.close()
    return row[0] if row else default

def set_pref(k: str, v: str):
    con = db(); cur = con.cursor()
    cur.execute("REPLACE INTO prefs(k,v) VALUES(?,?)", (k, v))
    con.commit(); con.close()

def mark_seen(feed: str, uid: str):
    con = db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO seen(feed,uid,ts) VALUES(?,?,?)",
                (feed, uid, int(time.time())))
    con.commit(); con.close()

def is_seen(feed: str, uid: str) -> bool:
    con = db(); cur = con.cursor()
    cur.execute("SELECT 1 FROM seen WHERE feed=? AND uid=?", (feed, uid))
    ok = cur.fetchone() is not None
    con.close(); return ok

# ── Помощници ────────────────────────────────────────────────────────────────
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
COINGECKO_SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

def pct_change(a: float, b: float) -> float:
    if a == 0: return 0.0
    return (b - a) / a * 100.0

def is_binance_symbol(sym: str) -> bool:
    return sym.endswith(("USDT","USDC","BUSD"))

def coingecko_id_for(sym: str) -> str | None:
    return DEFAULT_CG_IDS.get(sym.upper())

def fetch_price_binance(symbol: str) -> float | None:
    try:
        r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=7)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None

def fetch_price_coingecko_by_id(cg_id: str) -> float | None:
    try:
        r = requests.get(COINGECKO_SIMPLE_PRICE_URL, params={"ids": cg_id, "vs_currencies": "usd"}, timeout=7)
        r.raise_for_status()
        data = r.json()
        val = data.get(cg_id, {}).get("usd")
        return float(val) if val is not None else None
    except Exception:
        return None

def fetch_price(symbol: str) -> float | None:
    if is_binance_symbol(symbol):
        p = fetch_price_binance(symbol)
        if p is not None: return p
        cg = coingecko_id_for(symbol)
        return fetch_price_coingecko_by_id(cg) if cg else None
    cg = coingecko_id_for(symbol)
    return fetch_price_coingecko_by_id(cg) if cg else None

def host_from_link(link: str) -> str:
    try:
        return re.sub(r"^https?://", "", link).split("/")[0].lower()
    except Exception:
        return "source"

def classify_news(title: str, summary: str) -> str | None:
    text = f"{title} {summary}".lower()
    if not any(k in text for k in NEWS_KEYWORDS):
        return None
    pos = sum(k in text for k in POSITIVE)
    neg = sum(k in text for k in NEGATIVE)
    if neg > pos and neg > 0: return "🔴"
    if pos
