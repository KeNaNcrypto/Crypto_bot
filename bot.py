
import requests
import datetime
from telegram import Bot
import time

# 🔧 Настройки
TOKEN = '7725173875:AAGe-yLJxNGF3uuBOKVi_OjHsP6dJi2KZPU'
CHAT_ID = '5975002685'
bot = Bot(token=TOKEN)

# 💰 Пазарни нива
THRESHOLDS = {
    "shiba-inu": 0.000020,
    "snek": 0.0032,
    "treat": 0.0002,
    "doge-killer": 300,
    "bone-shibaswap": 0.7,
    "cardano": 0.4
}

# 📦 Състояние на сигналите
last_signals = {}

# 📊 Извличане на цена от CoinGecko
def get_price(coin_id):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
    res = requests.get(url).json()
    return res[coin_id]["usd"]

# 📤 Изпращане на сигнал
def send_signal(coin, action, price):
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    message = f"{now}\n🔔 *{action.upper()}* сигнал за *{coin.upper()}*\n💰 Цена: ${price:.8f}"
    bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')

# 🔍 Проверка за нови сигнали
def check_signals():
    for coin, level in THRESHOLDS.items():
        try:
            price = get_price(coin)
            action = None
            if price < level * 0.95:
                action = 'вход'
            elif price > level * 1.05:
                action = 'изход'
            # Ако има нов сигнал, различен от последния – пращаме
            if action and last_signals.get(coin) != action:
                send_signal(coin, action, price)
                last_signals[coin] = action
        except Exception as e:
            bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Грешка при {coin}: {str(e)}")

# 🔁 Основен цикъл – проверява на всеки 5 минути
print("Bot started. Monitoring every 5 minutes...")
while True:
    check_signals()
    time.sleep(300)  # 5 минути
