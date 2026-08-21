import os
import time
import json
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import requests

# --- НАСТРОЙКИ СКАЛЬПЕРА ---
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "c997ad22987e477e83034ea132621542")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8874872969:AAHtxvHw_mupom466pm3jh4BkZEjEAQ180A")

SYMBOL = "XAU/USD"
SIGNAL_FILE = "/tmp/scalp_signal.json"
DB_FILE = "subscribers.db"

# --- ПАРАМЕТРЫ РИСК-МЕНЕДЖМЕНТА (ЗАЩИТА ОТ ВЫБИВАНИЯ ПО СТОПАМ) ---
MIN_SL_DIST = 3.5  # Минимальный Стоп-Лосс ($3.5 = 35 пипсов)
MAX_SL_DIST = 12.0 # Максимальный Стоп-Лосс ($12.0 = 120 пипсов)
RR_RATIO = 2.0     # Риск/Прибыль 1:2

EMPTY_SIGNAL = {
    "symbol": "XAUUSD",
    "action": "NONE",
    "entry": 0.0,
    "sl": 0.0,
    "tp": 0.0,
    "status": "NONE",
    "timestamp": 0
}

lock = threading.Lock()

# ==========================================
# БАЗА ДАННЫХ ПОДПИСЧИКОВ И ПЕРСОНАЛЬНАЯ СВЯЗЬ
# ==========================================
def init_db():
    """Инициализация БД подписок"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id TEXT UNIQUE,
                is_active INTEGER DEFAULT 1,
                expire_date TEXT
            )
        ''')
        conn.commit()

def add_subscriber(telegram_id: str, days: int = 30):
    """Вспомогательная функция для добавления/продления подписчика"""
    expire_dt = datetime.now(timezone.utc) + timedelta(days=days)
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO users (telegram_id, is_active, expire_date)
            VALUES (?, 1, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                is_active = 1,
                expire_date = excluded.expire_date
        ''', (str(telegram_id), expire_dt.isoformat()))
        conn.commit()
    print(f"[+] Подписчик {telegram_id} активен до {expire_dt.isoformat()}")

def get_active_subscribers() -> list:
    """Получение списка Telegram Chat ID всех активных подписчиков"""
    active_ids = []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT telegram_id, expire_date FROM users WHERE is_active = 1")
            rows = cursor.fetchall()
            now_utc = datetime.now(timezone.utc)
            
            for telegram_id, expire_date_str in rows:
                try:
                    expire_dt = datetime.fromisoformat(expire_date_str)
                    if expire_dt > now_utc:
                        active_ids.append(telegram_id)
                    else:
                        cursor.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
                except Exception as ex:
                    print(f"[-] Ошибка парсинга даты пользователя {telegram_id}: {ex}")
            conn.commit()
    except Exception as e:
        print(f"[-] Ошибка чтения базы подписчиков: {e}")
    return active_ids

def broadcast_telegram_signal(text: str):
    """Персональная рассылка сигнала всем активным подписчикам по их chat_id"""
    if not TELEGRAM_BOT_TOKEN:
        print("[-] Ошибка: TELEGRAM_BOT_TOKEN не задан.")
        return

    subscribers = get_active_subscribers()
    if not subscribers:
        print("[ℹ️] Активных подписчиков для рассылки нет.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    for chat_id in subscribers:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        try:
            res = requests.post(url, json=payload, timeout=5)
            if res.status_code == 200:
                print(f"[+] Сигнал успешно доставлен пользователю {chat_id}")
            else:
                print(f"[-] Ошибка отправки пользователю {chat_id} ({res.status_code}): {res.text}")
        except Exception as e:
            print(f"[-] Исключение при отправке пользователю {chat_id}: {e}")

# ==========================================
# РАБОТА С СИГНАЛАМИ И ФАЙЛАМИ
# ==========================================
def save_signal_to_file(signal_data):
    try:
        with open(SIGNAL_FILE, "w") as f:
            json.dump(signal_data, f)
    except Exception as e:
        print(f"[-] Ошибка записи сигнала в файл: {e}")

def load_signal_from_file():
    if not os.path.exists(SIGNAL_FILE):
        return EMPTY_SIGNAL
    try:
        with open(SIGNAL_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return EMPTY_SIGNAL

def is_market_open():
    """Проверка окна 08:00 - 20:00 (Europe/Chisinau)"""
    now_local = datetime.now(ZoneInfo("Europe/Chisinau"))
    if now_local.weekday() >= 5:
        return False
    return 8 <= now_local.hour < 20

def fetch_tf_data(interval, retries=3):
    """Загрузка таймфрейма через TwelveData с повторными попытками при таймауте"""
    url = f"https://api.twelvedata.com/time_series?symbol={SYMBOL}&interval={interval}&outputsize=30&apikey={TWELVE_DATA_API_KEY}"
    headers = {'User-Agent': 'Mozilla/5.0'}

    for attempt in range(1, retries + 1):
        try:
            res = requests.get(url, headers=headers, timeout=(5, 15)).json()
            
            if "values" not in res:
                print(f"[-] Ошибка TwelveData на {interval} (попытка {attempt}/{retries}): {res.get('message', 'No values')}")
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
            print(f"[-] Исключение при загрузке {interval} (попытка {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(2)

    return interval, None

def get_multi_tf_market_data():
    """Последовательная загрузка таймфреймов с паузой 1.2с"""
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
    """Расчет динамического фильтра волатильности ATR"""
    high_low = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

def update_scalp_signal(action, entry, sl, tp, reason=""):
    signal = {
        "symbol": "XAUUSD",
        "action": action,
        "entry": float(entry),
        "sl": float(sl),
        "tp": float(tp),
        "status": "NEW",
        "timestamp": int(time.time())
    }
    with lock:
        save_signal_to_file(signal)
    
    now_str = datetime.now(ZoneInfo("Europe/Chisinau")).strftime('%H:%M:%S')
    print(f"⚡ [ПОЧАСОВОЙ СИГНАЛ - {reason}] {action} @ {entry} (SL: {sl}, TP: {tp})")

    # Формирование отчета для личной рассылки подписчикам
    icon = "🟢" if action == "BUY" else "🔴"
    msg = (
        f"⚡ <b>GOLD SCALPER SIGNAL</b> {icon}\n\n"
        f"<b>Инструмент:</b> XAU/USD (Gold)\n"
        f"<b>Действие:</b> <b>{action}</b>\n\n"
        f"📍 <b>Вход:</b> <code>{entry:.2f}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>{sl:.2f}</code> (Риск: {abs(entry - sl):.2f}$)\n"
        f"🎯 <b>Take Profit:</b> <code>{tp:.2f}</code> (Профит: {abs(entry - tp):.2f}$)\n\n"
        f"💡 <b>Стратегия:</b> {reason}\n"
        f"🕒 <i>Время сигнала: {now_str} (Кишинёв)</i>"
    )
    broadcast_telegram_signal(msg)

def generate_hourly_signal():
    """Генерация сигнала по multi-TF анализу"""
    try:
        if not is_market_open():
            now_local_str = datetime.now(ZoneInfo("Europe/Chisinau")).strftime('%H:%M:%S')
            print(f"[ℹ️] Вне рабочего окна (08:00 - 20:00). Текущее время: {now_local_str}")
            return

        now_str = datetime.now(ZoneInfo("Europe/Chisinau")).strftime('%H:%M:%S')
        print(f"🔍 [{now_str}] Старт Multi-TF скальп-анализа XAU/USD (M1, M5, M15, M30)...")

        df_m1, df_m5, df_m15, df_m30 = get_multi_tf_market_data()
        if df_m1 is None or df_m5 is None or df_m15 is None or df_m30 is None:
            print("[-] Ошибка: Не все таймфреймы были загружены. Пропуск скана.")
            return

        curr_price = round(float(df_m1['Close'].iloc[-1]), 2)
        m5_atr = calculate_atr(df_m5, 14) or 2.5

        # 1. Анализ тренда M30 / M15
        m30_ema = df_m30['Close'].tail(10).mean()
        m15_ema = df_m15['Close'].tail(10).mean()
        is_bullish = curr_price >= m30_ema and curr_price >= m15_ema

        # 2. SMC Sweep (M5/M1)
        m5_tail = df_m5.tail(12)
        m1_tail = df_m1.tail(5)
        local_high = float(m5_tail['High'].iloc[:-1].max())
        local_low = float(m5_tail['Low'].iloc[:-1].min())

        if float(m1_tail['Low'].min()) < local_low and is_bullish:
            raw_sl_dist = curr_price - (float(m1_tail['Low'].min()) - 1.0)
            sl_dist = max(MIN_SL_DIST, min(raw_sl_dist, MAX_SL_DIST, m5_atr * 2.0))
            sl = round(curr_price - sl_dist, 2)
            tp = round(curr_price + (sl_dist * RR_RATIO), 2)
            update_scalp_signal("BUY", curr_price, sl, tp, "M30-Trend + SMC Buy Sweep")
            return

        if float(m1_tail['High'].max()) > local_high and not is_bullish:
            raw_sl_dist = (float(m1_tail['High'].max()) + 1.0) - curr_price
            sl_dist = max(MIN_SL_DIST, min(raw_sl_dist, MAX_SL_DIST, m5_atr * 2.0))
            sl = round(curr_price + sl_dist, 2)
            tp = round(curr_price - (sl_dist * RR_RATIO), 2)
            update_scalp_signal("SELL", curr_price, sl, tp, "M30-Trend + SMC Sell Sweep")
            return

        # 3. Breakout M15
        range_max = float(df_m15.tail(4)['High'].max())
        range_min = float(df_m15.tail(4)['Low'].min())

        if curr_price >= range_max - 0.5 and is_bullish:
            raw_sl_dist = curr_price - (range_min - 0.5)
            sl_dist = max(MIN_SL_DIST, min(raw_sl_dist, MAX_SL_DIST))
            sl = round(curr_price - sl_dist, 2)
            tp = round(curr_price + (sl_dist * RR_RATIO), 2)
            update_scalp_signal("BUY", curr_price, sl, tp, "M15 Breakout High")
            return
        elif curr_price <= range_min + 0.5 and not is_bullish:
            raw_sl_dist = (range_max + 0.5) - curr_price
            sl_dist = max(MIN_SL_DIST, min(raw_sl_dist, MAX_SL_DIST))
            sl = round(curr_price - sl_dist, 2)
            tp = round(curr_price - (sl_dist * RR_RATIO), 2)
            update_scalp_signal("SELL", curr_price, sl, tp, "M15 Breakout Low")
            return

        # 4. Базовый сигнал по тренду
        sl_dist = max(MIN_SL_DIST, m5_atr * 1.8)
        if is_bullish:
            sl = round(curr_price - sl_dist, 2)
            tp = round(curr_price + (sl_dist * RR_RATIO), 2)
            update_scalp_signal("BUY", curr_price, sl, tp, "Hourly Trend BUY")
        else:
            sl = round(curr_price + sl_dist, 2)
            tp = round(curr_price - (sl_dist * RR_RATIO), 2)
            update_scalp_signal("SELL", curr_price, sl, tp, "Hourly Trend SELL")

    except Exception as e:
        print(f"[-] Исключение при выполнении generate_hourly_signal: {e}")

# --- СИНХРОНИЗИРОВАННЫЙ ТАЙМЕР (HH:01:00 UTC) ---
def get_seconds_until_next_hour():
    now = datetime.now(timezone.utc)
    if now.minute >= 1:
        target_time = (now + timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
    else:
        target_time = now.replace(minute=1, second=0, microsecond=0)
    return max((target_time - now).total_seconds(), 5)

def hourly_scheduler_loop():
    time.sleep(3)
    try:
        generate_hourly_signal()
    except Exception as e:
        print(f"[-] Ошибка при стартовом скане: {e}")

    while True:
        try:
            sleep_time = get_seconds_until_next_hour()
            print(f"⏳ Ожидание {round(sleep_time / 60, 1)} мин. ({int(sleep_time)} сек.) до следующего скана...")
            time.sleep(sleep_time)
            generate_hourly_signal()
        except Exception as e:
            print(f"[-] Ошибка в фоновом цикле таймера: {e}")
            time.sleep(10)

if __name__ == '__main__':
    init_db()
    
    # Пример тестового добавления администратора (замените '123456789' на ваш реальный Telegram Chat ID)
    add_subscriber("123456789", days=365)
    
    print("🚀 Скальпер запущен в автономном режиме персональной Telegram-рассылки...")
    hourly_scheduler_loop()
