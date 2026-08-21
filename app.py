import os
import time
import logging
import threading
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask import Flask
import pandas as pd
import requests

# ==========================================
# РЕГИСТРАЦИЯ И НАСТРОЙКА ЛОГИРОВАНИЯ
# ==========================================
log_formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
log_handler = RotatingFileHandler('xau_bot.log', maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
log_handler.setFormatter(log_formatter)

logger = logging.getLogger("XAUUSD_Scalper")
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# ==========================================
# ИНИЦИАЛИЗАЦИЯ И НАСТРОЙКИ
# ==========================================
app = Flask(__name__)

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "c997ad22987e477e83034ea132621542")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8874872969:AAHtxvHw_mupom466pm3jh4BkZEjEAQ180A")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@xauusd_scalp_signal")

SYMBOL_TWELVE = "XAU/USD"
SYMBOL_NAME = "XAUUSD"

MIN_SL_DIST = 3.5
MAX_SL_DIST = 12.0
RR_RATIO = 2.0

# ==========================================
# ТЕЛЕГРАМ УВЕДОМЛЕНИЯИ СИГНАЛЫ
# ==========================================
def send_telegram_signal(action, entry, sl, tp, reason=""):
    """Формирует и отправляет сигнал в Telegram-канал"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("[-] Не задан TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return

    chat_id = TELEGRAM_CHAT_ID if TELEGRAM_CHAT_ID.startswith("@") or TELEGRAM_CHAT_ID.startswith("-") else f"@{TELEGRAM_CHAT_ID}"
    now_str = datetime.now(ZoneInfo("Europe/Chisinau")).strftime('%H:%M:%S')
    icon = "🟢" if action == "BUY" else ("🔴" if action == "SELL" else "ℹ️")
    
    # Сообщение с красивой версткой для людей и строкой парсинга для cBot
    msg = (
        f"⚡ <b>GOLD SCALPER SIGNAL</b> {icon}\n\n"
        f"<b>Инструмент:</b> XAU/USD (Gold)\n"
        f"<b>Действие:</b> <b>{action}</b>\n\n"
        f"📍 <b>Вход:</b> <code>{entry:.2f}</code>\n"
        f"🛡 <b>Stop Loss:</b> <code>{sl:.2f}</code>\n"
        f"🎯 <b>Take Profit:</b> <code>{tp:.2f}</code>\n\n"
        f"💡 <b>Стратегия:</b> {reason}\n"
        f"🕒 <i>Время: {now_str} (Кишинёв)</i>\n\n"
        f"<code>SIGNAL:{action}|{SYMBOL_NAME}|{entry:.2f}|{sl:.2f}|{tp:.2f}</code>"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}
    
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            logger.info(f"✅ [ТЕЛЕГРАМ] Сигнал {action} {entry} успешно отправлен в {chat_id}")
        else:
            logger.error(f"[-] Ошибка отправки в Telegram ({res.status_code}): {res.text}")
    except Exception as e:
        logger.error(f"[-] Исключение при отправке в Telegram: {e}")

# ==========================================
# РАСЧЕТ АНАЛИТИКИ ТРЕЙДИНГА
# ==========================================
def is_market_open():
    """Проверка окна торговых сессий 08:00 - 20:00 (Europe/Chisinau)"""
    now_local = datetime.now(ZoneInfo("Europe/Chisinau"))
    if now_local.weekday() >= 5:
        return False
    return 8 <= now_local.hour < 20

def fetch_tf_data(interval, retries=3):
    url = f"https://api.twelvedata.com/time_series?symbol={SYMBOL_TWELVE}&interval={interval}&outputsize=30&apikey={TWELVE_DATA_API_KEY}"
    headers = {'User-Agent': 'Mozilla/5.0'}

    for attempt in range(1, retries + 1):
        try:
            res = requests.get(url, headers=headers, timeout=(5, 15)).json()
            if "values" not in res:
                if attempt < retries:
                    time.sleep(2)
                    continue
                return interval, None

            df = pd.DataFrame(res["values"])
            for col in ['open', 'high', 'low', 'close']:
                df[col] = df[col].astype(float)
            df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
            df.iloc[:] = df.iloc[::-1].values
            return interval, df
        except Exception as e:
            if attempt < retries:
                time.sleep(2)

    return interval, None

def get_multi_tf_market_data():
    intervals = ["1min", "5min", "15min", "30min"]
    dfs = {}
    for interval in intervals:
        _, df = fetch_tf_data(interval)
        if df is None:
            return None, None, None, None
        dfs[interval] = df
        time.sleep(1.2)

    return dfs["1min"], dfs["5min"], dfs["15min"], dfs["30min"]

def calculate_atr(df, period=14):
    high_low = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

def generate_hourly_signal():
    """Генерация сигнала по multi-TF анализу"""
    try:
        if not is_market_open():
            logger.info("[ℹ️] Вне рабочего окна (08:00 - 20:00). Скан пропущен.")
            return

        logger.info("🔍 Старт Multi-TF скальп-анализа XAU/USD...")
        df_m1, df_m5, df_m15, df_m30 = get_multi_tf_market_data()
        
        if df_m1 is None or df_m5 is None or df_m15 is None or df_m30 is None:
            logger.error("[-] Ошибка: Не все таймфреймы загружены.")
            return

        curr_price = round(float(df_m1['Close'].iloc[-1]), 2)
        m5_atr = calculate_atr(df_m5, 14) or 2.5

        is_bullish = curr_price >= df_m30['Close'].tail(10).mean() and curr_price >= df_m15['Close'].tail(10).mean()

        m5_tail = df_m5.tail(12)
        m1_tail = df_m1.tail(5)
        local_high = float(m5_tail['High'].iloc[:-1].max())
        local_low = float(m5_tail['Low'].iloc[:-1].min())

        # SMC Sweep
        if float(m1_tail['Low'].min()) < local_low and is_bullish:
            sl_dist = max(MIN_SL_DIST, min(curr_price - (float(m1_tail['Low'].min()) - 1.0), MAX_SL_DIST, m5_atr * 2.0))
            send_telegram_signal("BUY", curr_price, round(curr_price - sl_dist, 2), round(curr_price + (sl_dist * RR_RATIO), 2), "M30-Trend + SMC Buy Sweep")
            return

        if float(m1_tail['High'].max()) > local_high and not is_bullish:
            sl_dist = max(MIN_SL_DIST, min((float(m1_tail['High'].max()) + 1.0) - curr_price, MAX_SL_DIST, m5_atr * 2.0))
            send_telegram_signal("SELL", curr_price, round(curr_price + sl_dist, 2), round(curr_price - (sl_dist * RR_RATIO), 2), "M30-Trend + SMC Sell Sweep")
            return

        # Базовый трендовый сигнал
        sl_dist = max(MIN_SL_DIST, m5_atr * 1.8)
        if is_bullish:
            send_telegram_signal("BUY", curr_price, round(curr_price - sl_dist, 2), round(curr_price + (sl_dist * RR_RATIO), 2), "Hourly Trend BUY")
        else:
            send_telegram_signal("SELL", curr_price, round(curr_price + sl_dist, 2), round(curr_price - (sl_dist * RR_RATIO), 2), "Hourly Trend SELL")

    except Exception as e:
        logger.error(f"[-] Исключение при анализе рынка: {e}")

# ==========================================
# ТАЙМЕР
# ==========================================
def hourly_scheduler_loop():
    time.sleep(3)
    # Отправка тестового сигнала при старте
    send_telegram_signal("BUY", 2700.00, 2690.00, 2720.00, "Тестовый запуск сервера")

    while True:
        try:
            now = datetime.now(timezone.utc)
            target_time = (now + timedelta(hours=1)).replace(minute=59, second=0, microsecond=0) if now.minute >= 59 else now.replace(minute=59, second=0, microsecond=0)
            sleep_time = max((target_time - now).total_seconds(), 5)
            
            logger.info(f"⏳ Ожидание {round(sleep_time / 60, 1)} мин. до следующего скана...")
            time.sleep(sleep_time)
            generate_hourly_signal()
        except Exception as e:
            logger.error(f"[-] Ошибка в таймере: {e}")
            time.sleep(10)

if __name__ == '__main__':
    threading.Thread(target=hourly_scheduler_loop, daemon=True).start()
    port = int(os.environ.get('PORT', 5001))
    logger.info(f"🚀 Сервер XAUUSD Scalper запущен на порту {port}...")
    app.run(host='0.0.0.0', port=port)
