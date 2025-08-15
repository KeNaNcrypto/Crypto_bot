# bot.py — Един файл: само сигнали за РЪСТ (≤5 мин) + важни новини (🟢/🔴)
# Работи с Binance + CoinGecko (LEASH, BONE, TREAT, SNEK и др.)
# НЕ е нужно да редактираш кода. Използва ENV променливи от стария бот:
# TELEGRAM_BOT_TOKEN, OWNER_CHAT_ID. Останалото има разумни стойности.
#
# Команди от чата (без да пипаш код):
#   /status               – статус, символи, настроен процент
#   /set_rise 7           – сменя праг за ръст (в %) без рестарт
#   /rise_on, /rise_off   – вкл/изкл сигналите за ръст
#   /set_symbols BTCUSDT,ETHUSDT,LEASH,BONE,TREAT,SNEK  – сменя списъка
#   /news_on, /news_off   – вкл/изкл важните новини
#
# Зависимости (както при предишните версии):
#   python-telegram-bot==21.6, requests==2.32.3, feedparser==6.0.11

import os, time, asyncio, re, sqlite3
from collections import deque, defaultdict

import requests
import feedparser
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ───────────────────────────
# Основни настройки (ENV)
# ───────────────────────────
TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))

# Символи: можеш да ги смениш по-късно от чата с /set_symbols
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,ADAUSDT,LEASH,BONE,TREAT,SNEK"
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()]

# Ръст-праг (в %) – може да се сменя в движение с /set_rise 7
DEFAULT_RISE_PCT = float(os.getenv("RISE_PCT", "5"))
ENABLE_RISE_ALERTS = os.getenv("ENABLE_RISE_ALERTS", "1") == "1"  # по подразбиране ВКЛ
PRICE_POLL_SECONDS = max(int(os.getenv("PRICE_POLL_SECONDS", "30")), 10)
COOLDOWN_MINUTES = max(int(os.getenv("COOLDOWN_MINUTES", "10")), 1)

# Новини (по подразбиране ВКЛ) – /news_off за изключване
NEWS_POLL_INTERVAL = max(int(os.getenv("NEWS_POLL_INTERVAL", "90")), 30)
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

# CoinGecko ID mapping (за монети извън Binance)
DEFAULT_CG_IDS = {
    "LEASH": "doge-killer",
    "BONE": "bone-shibaswap",
    "SNEK": "snek",
    "TREAT": "treat",  # ако твоят е различен актив – /set_symbols ще го задържи, а mapping може да се допълни по-късно
}

# ───────────────────────────
# Вътрешно състояние
# ───────────────────────────
WINDOW_SECONDS = 5 * 60  # прозорец за "≤5 мин"
price_window: dict[str, deque] = {sym: deque() for sym in SYMBOLS}
last_alert_up: dict[str, float] = defaultdict(lambda: 0.0)
current_rise_pct: float = DEFAULT_RISE_PCT  # променя се с /set_rise

# Новини: SQLite (видяно + преференции)
DB_PATH = "news_store.sqlite"

def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    con = db(); cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen(
            feed TEXT,
            uid  TEXT,
            ts   INTEGER,
            PRIMARY KEY(feed, uid)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS prefs(
            k TEXT PRIMARY KEY,
            v TEXT
        )
    """)
    cur.execute("INSERT OR IGNORE INTO prefs(k,v) VALUES('news_enabled', ?)", ("1" if NEWS_ENABLED_DEFAULT else "0",))
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

# ───────────────────────────
# Помощници
# ───────────────────────────
def pct_change(a: float, b: float) -> float:
    if a == 0: return 0.0
    return (b - a) / a * 100.0

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
    if pos > neg and pos > 0: return "🟢"
    return None

# ───────────────────────────
# Цена: Binance + CoinGecko
# ───────────────────────────
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
COINGECKO_SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

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
        if p is not None:
            return p
        # fallback към CG (рядко)
        cg = coingecko_id_for(symbol)
        return fetch_price_coingecko_by_id(cg) if cg else None
    else:
        cg = coingecko_id_for(symbol)
        return fetch_price_coingecko_by_id(cg) if cg else None

# ───────────────────────────
# Луп за РЪСТ (само нагоре)
# ───────────────────────────
async def prices_loop(app: Application):
    global price_window, current_rise_pct
    cooldown = COOLDOWN_MINUTES * 60
    await asyncio.sleep(2)

    while True:
        t0 = time.time()
        for sym in list(SYMBOLS):
            price = fetch_price(sym)
            if price is None:
                continue

            dq = price_window.setdefault(sym, deque())
            dq.append((t0, price))
            # чистим > 5 мин
            while dq and (t0 - dq[0][0] > 5*60):
                dq.popleft()

            if not dq or not ENABLE_RISE_ALERTS:
                continue

            # РЪСТ спрямо МИНИМУМА в прозореца (≤5 мин)
            min_p = min(p for _, p in dq)
            rise = pct_change(min_p, price)
            if rise >= abs(current_rise_pct):
                if t0 - last_alert_up[sym] >= cooldown:
                    last_alert_up[sym] = t0
                    msg = (f"🔺 {sym}: {rise:.2f}% ръст ≤5 мин\n"
                           f"От ~{min_p:.10g} до {price:.10g}")
                    try:
                        await app.bot.send_message(CHAT_ID, msg)
                    except Exception:
                        pass

        elapsed = time.time() - t0
        await asyncio.sleep(max(1, PRICE_POLL_SECONDS - int(elapsed)))

# ───────────────────────────
# Новини (важни само)
# ───────────────────────────
async def news_loop(app: Application):
    # анти-спам при старт: маркираме най-новите като видяни
    for feed in FEEDS_DEFAULT:
        try:
            d = feedparser.parse(feed)
            if d.entries:
                e0 = d.entries[0]
                uid = getattr(e0, "id", "") or getattr(e0, "guid", "") or getattr(e0, "link", "") or getattr(e0, "title", "")
                if uid:
                    mark_seen(feed, uid)
        except Exception:
            pass

    await asyncio.sleep(NEWS_POLL_INTERVAL)

    while True:
        try:
            if get_pref("news_enabled", "1") == "1":
                for feed in FEEDS_DEFAULT:
                    try:
                        d = feedparser.parse(feed)
                    except Exception:
                        continue
                    # от старите към новите (да пазим ред)
                    for e in reversed(d.entries[:10]):
                        uid = getattr(e, "id", "") or getattr(e, "guid", "") or getattr(e, "link", "") or getattr(e, "title", "")
                        if not uid or is_seen(feed, uid):
                            continue
                        title = getattr(e, "title", "")
                        summary = getattr(e, "summary", "")
                        link = getattr(e, "link", "")
                        tag = classify_news(title, summary)
                        if tag:
                            msg = f"{tag} <b>{title}</b>\n🔗 <a href=\"{link}\">{host_from_link(link)}</a>"
                            try:
                                await app.bot.send_message(CHAT_ID, msg, parse_mode="HTML", disable_web_page_preview=True)
                            except Exception:
                                pass
                        mark_seen(feed, uid)
        except asyncio.CancelledError:
            break
        except Exception:
            pass
        await asyncio.sleep(NEWS_POLL_INTERVAL)

# ───────────────────────────
# Команди
# ───────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "✅ Ботът е активен.\n"
        "• Сигнали: само за РЪСТ (≤5 мин), праг в % се сменя с /set_rise\n"
        "• Новини: 🟢/🔴 от CoinDesk, Cointelegraph, Binance, SEC, ФЕД\n\n"
        "Команди:\n"
        "/status – статус и текущи символи\n"
        "/set_rise 7 – сменя прага (в %), без код\n"
        "/rise_on, /rise_off – вкл/изкл сигналите за ръст\n"
        "/set_symbols BTCUSDT,ETHUSDT,LEASH,BONE,TREAT,SNEK – сменя списъка\n"
        "/news_on, /news_off – вкл/изкл новините"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    syms = ", ".join(SYMBOLS)
    news = "ВКЛ" if get_pref("news_enabled","1") == "1" else "ИЗКЛ"
    rise = "ВКЛ" if ENABLE_RISE_ALERTS else "ИЗКЛ"
    await update.effective_chat.send_message(
        f"📊 Символи: {syms}\n"
        f"⏱ Опашка: 5мин | Интервал: {PRICE_POLL_SECONDS}s | Ръст: {current_rise_pct:.1f}% [{rise}]\n"
        f"📰 Новини: {news} | Интервал: {NEWS_POLL_INTERVAL}s"
    )

async def cmd_set_rise(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global current_rise_pct
    try:
        val = float(ctx.args[0])
        if val <= 0 or val > 100:
            raise ValueError
        current_rise_pct = val
        await update.effective_chat.send_message(f"✅ Прагът за ръст е {current_rise_pct:.2f}% (≤5 мин).")
    except Exception:
        await update.effective_chat.send_message("Използване: /set_rise 5  (число от 0.1 до 100)")

async def cmd_rise_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ENABLE_RISE_ALERTS
    ENABLE_RISE_ALERTS = True
    await update.effective_chat.send_message("✅ Алармите за ръст са ВКЛ.")

async def cmd_rise_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ENABLE_RISE_ALERTS
    ENABLE_RISE_ALERTS = False
    await update.effective_chat.send_message("🛑 Алармите за ръст са ИЗКЛ.")

async def cmd_set_symbols(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global SYMBOLS, price_window
    body = update.message.text.partition(" ")[2].strip().upper()
    if not body:
        await update.effective_chat.send_message("Използване: /set_symbols BTCUSDT,ETHUSDT,LEASH,BONE,TREAT,SNEK")
        return
    new_syms = [s.strip() for s in body.split(",") if s.strip()]
    if not new_syms:
        await update.effective_chat.send_message("Не са подадени валидни символи.")
        return
    SYMBOLS[:] = new_syms
    for s in new_syms:
        price_window.setdefault(s, deque())
    await update.effective_chat.send_message("✅ Символите са обновени: " + ", ".join(SYMBOLS))

async def cmd_news_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_pref("news_enabled", "1")
    await update.effective_chat.send_message("✅ Новините са ВКЛ.")

async def cmd_news_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_pref("news_enabled", "0")
    await update.effective_chat.send_message("🛑 Новините са ИЗКЛ.")

# ───────────────────────────
# Стартиране
# ───────────────────────────
def main():
    if not TOKEN or CHAT_ID == 0:
        raise SystemExit("❌ Липсва TELEGRAM_BOT_TOKEN или OWNER_CHAT_ID в средата.")

    init_db()

    app = Application.builder().token(TOKEN).build()
    # Команди
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("set_rise",   cmd_set_rise))
    app.add_handler(CommandHandler("rise_on",    cmd_rise_on))
    app.add_handler(CommandHandler("rise_off",   cmd_rise_off))
    app.add_handler(CommandHandler("set_symbols",cmd_set_symbols))
    app.add_handler(CommandHandler("news_on",    cmd_news_on))
    app.add_handler(CommandHandler("news_off",   cmd_news_off))

    # Фонови цикли
    app.job_queue.run_once(lambda *_: app.create_task(prices_loop(app)), when=1)
    app.job_queue.run_once(lambda *_: app.create_task(news_loop(app)),   when=1)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
