import requests
import time
import logging
from datetime import datetime
from telegram import Bot

# Настройки
TOKEN = '7725173875:AAGe-yLJxNGF3uuBOKVi_OjHsP6dJi2KZPU'
CHAT_ID = '5975002685'
bot = Bot(token=TOKEN)

# Криптовалути и CoinGecko ID-та
COINS = {
    'SHIB': 'shiba-inu',
    'SNEK': 'snek',
    'TREAT': 'treat-token',
    'LEASH': 'doge-killer',
    'BONE': 'bone-shibaswap',
    'ADA': 'cardano',
}

# Стара цена за сравнение
last_prices = {}

# Интервал за проверка (в секунди)
CHECK_INTERVAL = 180  # 3 минути

def get_price(coin_id):
    url = f'https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd'
    try:
        response = requests.get(url)
        return response.json()[coin_id]['usd']
    except Exception as e:
        logging.error(f"Грешка при вземане на цена: {e}")
        return None

def send_signal(symbol, price, signal_type):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    message = f"📈 [{now} UTC] {signal_type} сигнал за {symbol}\\n💰 Цена: ${price:.8f}"
    bot.send_message(chat_id=CHAT_ID, text=message)

def main_loop():
    while True:
        for symbol, coin_id in COINS.items():
            price = get_price(coin_id)
            if price is None:
                continue

            if symbol not in last_prices:
                last_prices[symbol] = price
                continue

            last_price = last_prices[symbol]
            change = (price - last_price) / last_price

            if change >= 0.02:
                send_signal(symbol, price, "🟢 Вход")
            elif change <= -0.02:
                send_signal(symbol, price, "🔴 Изход")

            last_prices[symbol] = price
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    main_loop()
