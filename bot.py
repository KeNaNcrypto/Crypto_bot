# REQUIREMENTS (в requirements.txt):
# pyTelegramBotAPI==4.17.0
# requests==2.32.3
# feedparser==6.0.11

import os, time, math, threading, logging
from datetime import datetime, timedelta, timezone
from collections import deque, defaultdict

import requests
import feedparser
import telebot

# -----------------------------
# 1) ENV от Render (Settings → Environment)
# -----------------------------
BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_CHAT_ID  = int(os.getenv("OWNER_CHAT_ID", "0"))

# Монети за следене (Coingecko ids)
COINS = {
    "SHIB":  "shiba-inu",
    "LEASH": "doge-killer",
    "BONE":  "bone-shibaswap",
    "ADA":   "cardano",
    "SNEK":  "snek",  # ако го няма в Coingecko, просто ще се пропуска
    # "TREAT": "treat",  # добави ако има точен id в Coingecko
}

# Праг за 5-мин сигнал ±X%
PCT_5M_THRESHOLD = float(os.getenv("PCT_5M", "5"))

# Интервали
PRICE_POLL_SEC   = int(os.getenv("PRICE_POLL_SEC", "60"))   # проверка на 60 сек
NEWS_POLL_SEC    = int(os.getenv("NEWS_POLL_SEC", "300"))   # новини на 5 мин

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# -----------------------------
# 2) Кеш за цени и анти-спам
# -----------------------------
price_history = {sym: deque(maxlen=15) for sym in COINS}  # ~15 мин история при 60 сек
last_signal_at = defaultdict(lambda: datetime(1970,1,1, tzinfo=timezone.utc))
MIN_SIG_GAP = timedelta(minutes=10)

# Новини: пазим последните заглавия за анти-дубликат
sent_news = deque(maxlen=100)

# -----------------------------
# 3) Помощни
# -----------------------------
def now_utc():
    return datetime.now(timezone.utc)

def send_denied(chat_id):
    # кратко инфо за непознати чатове
    try:
        bot.send_message(chat_id, "❌ Нямаш достъп до този бот.")
    except Exception:
        pass

def safe_send(chat_id, text):
    if chat_id != OWNER_CHAT_ID:
        logging.warning(f"Блокирано -> чужд chat_id={chat_id}")
        return
    try:
        bot.send_message(chat_id, text)
    except Exception as e:
        logging.error(f"send_message error: {e}")

def cg_simple_price(ids):
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": ",".join(ids), "vs_currencies": "usd"}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def cg_market_chart(coin_id, days="1", interval="minute"):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": days, "interval": interval}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("prices", [])

def ema(series, period):
    if len(series) < period: return []
    k = 2 / (period + 1)
    out = []
    sma = sum(series[:period]) / period
    out.extend([None]*(period-1))
    out.append(sma)
    prev = sma
    for p in series[period:]:
        val = (p - prev) * k + prev
        out.append(val)
        prev = val
    return out

def rsi(series, period=14):
    # стандартна RSI; връща списък със стойности и None за първите period елемента
    if len(series) < period + 1: return []
    gains, losses = [], []
    for i in range(1, period+1):
        ch = series[i] - series[i-1]
        gains.append(max(ch, 0.0))
        losses.append(-min(ch, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rsis = [None]*period
    for i in range(period+1, len(series)):
        ch = series[i] - series[i-1]
        gain = max(ch, 0.0)
        loss = -min(ch, 0.0)
        avg_gain = (avg_gain*(period-1) + gain) / period
        avg_loss = (avg_loss*(period-1) + loss) / period
        rs = math.inf if avg_loss == 0 else (avg_gain / avg_loss)
        rsis.append(100 - (100/(1+rs)))
    return rsis

def format_usd(x):
    if x >= 1: return f"${x:,.2f}"
    return f"${x:.8f}".rstrip('0').rstrip('.')

def percent_change(current, old):
    if old == 0: return 0.0
    return (current - old) / old * 100.0

# -----------------------------
# 4) Вход/Изход логика
# -----------------------------
def entry_exit_signal(closes):
    """
    BUY:  EMA20 > EMA50 и RSI(14) кръстосва нагоре 30.
    SELL: RSI(14) кръстосва надолу 70 или close < EMA50.
    """
    if len(closes) < 80:
        return None

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    r = rsi(closes, 14)

    # защити за ръбове при RSI
    if not r or len(r) < 2 or r[-1] is None or r[-2] is None:
        return None

    c = closes[-1]
    e20 = ema20[-1] if ema20 else None
    e50 = ema50[-1] if ema50 else None
    r_last = r[-1]
    r_prev = r[-2]

    # BUY
    if (e20 is not None) and (e50 is not None):
        if e20 > e50 and r_prev < 30 <= r_last:
            return f"🟢 Вход (RSI↑>30, EMA20>EMA50) | Цена: {format_usd(c)}"

    # SELL
    if r_prev > 70 >= r_last:
        return f"🔴 Изход (RSI↓<70) | Цена: {format_usd(c)}"
    if (e50 is not None) and c < e50:
        return f"🔴 Изход (под EMA50) | Цена: {format_usd(c)}"

    return None

# -----------------------------
# 5) Цени – цикъл
# -----------------------------
def prices_loop():
    while True:
        try:
            ids = list(COINS.values())
            if not ids:
                time.sleep(PRICE_POLL_SEC)
                continue

            data = cg_simple_price(ids)
            ts = now_utc()

            for sym, cid in COINS.items():
                price = data.get(cid, {}).get("usd")
                if price is None:
                    logging.warning(f"[{sym}] няма цена от Coingecko")
                    continue
                price = float(price)
                price_history[sym].append((ts, price))

                # 5-мин сигнал
                dq = price_history[sym]
                old = None
                for t0, p0 in dq:
                    if ts - t0 >= timedelta(minutes=5):
                        old = (t0, p0)  # най-близкото >=5м назад
                if old:
                    pct = percent_change(price, old[1])
                    if abs(pct) >= PCT_5M_THRESHOLD:
                        key = f"{sym}_pct5m_{'up' if pct>0 else 'down'}"
                        if now_utc() - last_signal_at[key] > MIN_SIG_GAP:
                            arrow = "🟢" if pct > 0 else "🔴"
                            safe_send(OWNER_CHAT_ID, f"{arrow} {sym}: {pct:+.2f}% за 5 мин | Текущо: {format_usd(price)}")
                            last_signal_at[key] = now_utc()

                # Вход/Изход сигнал от минутни данни (посл. 1 ден)
                try:
                    prices = cg_market_chart(cid, days="1", interval="minute")
                    closes = [float(x[1]) for x in prices if isinstance(x, (list, tuple)) and len(x) >= 2]
                    sig = entry_exit_signal(closes)
                    if sig:
                        key2 = f"{sym}_ee"
                        if now_utc() - last_signal_at[key2] > MIN_SIG_GAP:
                            safe_send(OWNER_CHAT_ID, f"{sym} | {sig}")
                            last_signal_at[key2] = now_utc()
                except Exception as e:
                    logging.warning(f"EE calc fail {sym}: {e}")

        except Exception as e:
            logging.error(f"prices_loop error: {e}")

        time.sleep(PRICE_POLL_SEC)

# -----------------------------
# 6) Новини – цикъл (важни ключови думи)
# -----------------------------
NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://cointelegraph.com/rss",
]
NEWS_KEYWORDS = [
    "SEC", "ETF", "hack", "exploit", "security breach", "listing",
    "lawsuit", "ban", "regulation", "court", "Fed", "interest rate",
    "Binance", "Coinbase", "BlackRock", "Grayscale", "ETF approval",
    "liquidation", "outage", "fork", "halt",
]

def important_news(title: str) -> bool:
    t = title.lower()
    for k in NEWS_KEYWORDS:
        if k.lower() in t:
            return True
    return False

def news_loop():
    while True:
        try:
            for feed in NEWS_FEEDS:
                d = feedparser.parse(feed)
                for e in d.entries[:10]:
                    title = e.get("title", "")
                    link = e.get("link", "")
                    if not title or not link:
                        continue
                    key = (title.strip(), link.strip())
                    if key in sent_news:
                        continue
                    if important_news(title):
                        safe_send(OWNER_CHAT_ID, f"📰 Важна новина: {title}\n{link}")
                        sent_news.append(key)
        except Exception as e:
            logging.error(f"news_loop error: {e}")

        time.sleep(NEWS_POLL_SEC)

# -----------------------------
# 7) Telegram handlers
# -----------------------------
@bot.message_handler(commands=['start'])
def start_cmd(msg):
    if msg.chat.id != OWNER_CHAT_ID:
        send_denied(msg.chat.id)
        return
    safe_send(msg.chat.id,
        "✅ Ботът е активен.\n"
        "Команди:\n"
        "/id – твоето chat_id\n"
        "/price <COIN> – текуща цена (напр. /price LEASH)\n"
        "/ping – тест\n"
        f"5-мин сигнал: ±{PCT_5M_THRESHOLD}%\n"
        "Сигнали Вход/Изход: RSI/EMA.\n"
        "Новини: филтър по важни ключови думи."
    )

@bot.message_handler(commands=['id'])
def id_cmd(msg):
    if msg.chat.id != OWNER_CHAT_ID:
        send_denied(msg.chat.id)
        return
    safe_send(msg.chat.id, f"chat_id: {msg.chat.id}")

@bot.message_handler(commands=['ping'])
def ping_cmd(msg):
    if msg.chat.id != OWNER_CHAT_ID:
        send_denied(msg.chat.id)
        return
    safe_send(msg.chat.id, "pong")

@bot.message_handler(commands=['price'])
def price_cmd(msg):
    if msg.chat.id != OWNER_CHAT_ID:
        send_denied(msg.chat.id)
        return
    parts = msg.text.strip().split()
    if len(parts) < 2:
        safe_send(msg.chat.id, "Използвай: /price COIN (напр. /price LEASH)")
        return
    sym = parts[1].upper()
    cid = COINS.get(sym)
    if not cid:
        safe_send(msg.chat.id, f"Непозната монета: {sym}")
        return
    try:
        data = cg_simple_price([cid])
        px = float(data[cid]["usd"])
        safe_send(msg.chat.id, f"{sym}: {format_usd(px)}")
    except Exception as e:
        safe_send(msg.chat.id, f"Грешка при цена за {sym}: {e}")

# -----------------------------
# 8) Стартиране – паралелни цикли
# -----------------------------
def run_bot():
    while True:
        try:
            bot.polling(none_stop=True, timeout=60)
        except Exception as e:
            logging.error(f"polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    if not BOT_TOKEN or not OWNER_CHAT_ID:
        raise SystemExit("Липсва TELEGRAM_BOT_TOKEN или OWNER_CHAT_ID в Environment!")
    threading.Thread(target=prices_loop, daemon=True).start()
    threading.Thread(target=news_loop, daemon=True).start()
    logging.info("Ботът стартира (ценови и новинарски цикли активни).")
    run_bot()
