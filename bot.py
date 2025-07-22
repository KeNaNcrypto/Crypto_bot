import requests
import time
import logging
from datetime import datetime
from telegram import Bot
import pandas as pd

# Настройки
TOKEN = '7725173875:AAGe-yLJxNGF3uuBOKVi_OjHsP6dJi2KZPU'
CHAT_ID = '5975002685'
bot = Bot(token=TOKEN)

COINS = {
    'SHIB': 'shiba-inu',
    'SNEK': 'snek',
    'TREAT': 'treat-token',
    'LEASH': 'doge-killer',
    'BONE': 'bone-shibaswap',
    'ADA': 'cardano',
}

INTERVAL = 180  # 3 минути

def get_ohlc(coin_id):
    url = f'https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days=1&interval=hourly'
    try:
        res = requests.get(url).json()
        prices = res['prices'][-30:]  # последните 30 часа
        df = pd.DataFrame(prices, columns=['timestamp', 'price'])
        df['price'] = df['price'].astype(float)
        return df
    except Exception as e:
        logging.error(f"Грешка при ohlc: {e}")
        return None

def calculate_indicators(df):
    df['EMA9'] = df['price'].ewm(span=9, adjust=False).mean()
    df['EMA21'] = df['price'].ewm(span=21, adjust=False).mean()
    delta = df['price'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

def analyze(df):
    if df is None or len(df) < 21:
        return None

    last = df.iloc[-1]
    ema9 = last['EMA9']
    ema21 = last['EMA21']
    rsi = last['RSI']

    if rsi < 30 and ema9 > ema21:
        return "🟢 Вход (RSI < 30 и EMA9 > EMA21)"
    elif rsi > 70 or ema9 < ema21:
        return "🔴 Изход (RSI >
