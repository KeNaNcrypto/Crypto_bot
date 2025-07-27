import time
import requests
from collections import deque
from telegram import Bot

# 🔐 Реални данни
TOKEN = '8375149420:AAEp6fFoDpfEyd8VGwK5a7YUAG2hbqfKqBY'
CHAT_ID = '5975002685'

bot = Bot(token=TOKEN)

# Криптовалути за следене
tracked_symbols = ["SHIB", "SNEK", "TREAT", "LEASH", "BONE", "ADA"]

# История на последните 5 цени (1 запис/минута)
price_history = {symbol: deque(maxlen=5) for symbol in tracked_symbols}

# Преобразуване към CoinGecko ID
symbol_map = {
    "SHIB": "shiba-inu",
    "SNEK": "snek",
    "TREAT": "treat",
    "LEASH": "doge-killer",
    "BONE": "bone-shibaswap",
    "ADA": "cardano"
}

# Вземане на текуща цена
def get_price(symbol):
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={symbol_map[symbol]}&vs_currencies=usd"
        response = requests.get(url)
        data = response.json()
        return float(data[symbol_map[symbol]]['usd'])
    except Exception as e:
        print(f"❌ Грешка при вземане на цена за {symbol}: {e}")
        return None

# Проверка за скок ≥5% за 5 минути
def check_price_spike(symbol, current_price):
    history = price_history[symbol]
    if len(history) == 5:
        old_price = history[0]
        percent_change = ((current_price - old_price) / old_price) * 100
        if percent_change >= 5:
            return True, percent_change
    history.append(current_price)
    return False, 0

# Главен цикъл
def run_bot():
    while True:
        for symbol in tracked_symbols:
            current_price = get_price(symbol)
            if current_price is None:
                continue

            spike, percent = check_price_spike(symbol, current_price)
            if spike:
                message = f"🚀 {symbol}: Цената се покачи с {percent:.2f}% за последните 5 минути!"
                bot.send_message(chat_id=CHAT_ID, text=message)

        time.sleep(60)

if __name__ == "__main__":
    run_bot()
