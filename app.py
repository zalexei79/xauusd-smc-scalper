import os
import time
import json
import sqlite3
import logging
import threading
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
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
# ИНИЦИАЛИЗАЦИЯ И НАСТРОЙКИ ПРИЛОЖЕНИЯ
# ==========================================
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "xauusd_secret_key_2026")

# Flask-SocketIO с поддержкой CORS
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "c997ad22987e477e83034ea132621542")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8874872969:AAHtxvHw_mupom466pm3jh4BkZEjEAQ180A")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@xauusd_scalp_signal")

SYMBOL_TWELVE = "XAU/USD"
ROOM_NAME = "XAUUSD"
DB_PATH = "subscriptions.db"

MIN_SL_DIST = 3.5
MAX_SL_DIST = 12.0
RR_RATIO = 2.0

lock = threading.Lock()

# ==========================================
# БАЗА ДАННЫХ И АВТОРИЗАЦИЯ
# ==========================================
def init_db():
    """Автоматическая инициализация SQLite базы данных для подписок"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key TEXT UNIQUE NOT NULL,
                account_id TEXT NOT NULL,
                telegram_id TEXT,
                expires_at TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            )
        ''')
        
        cursor.execute("SELECT COUNT(*) FROM users")
        if cursor.fetchone()[0] == 0:
            cursor.execute('''
                INSERT INTO users (api_key, account_id, telegram_id, expires_at, is_active)
                VALUES ('TEST_KEY_123', '12345678', '00000000', '2030-01-01T00:00:00Z', 1)
            ''')
            logger.info("Создана тестовая учётная запись: API_KEY='TEST_KEY_123', Account='12345678'")
            
        conn.commit()
        conn.close()
        logger.info("База данных subscriptions.db успешно проверена/инициализирована.")
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {e}")

def validate_client(api_key, account_id):
    """Проверка лицензионного ключа, привязанного счета и срока действия подписки"""
    if not api_key or not account_id:
        return False, "Отсутствуют авторизационные данные (api_key / account_id)"
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT account_id, expires_at, is_active FROM users WHERE api_key = ?
        ''', (api_key,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return False, "Неверный API-ключ"

        db_account_id, expires_at_str, is_active = row

        if not is_active:
            return False, "Подписка деактивирована"

        if str(db_account_id) != str(account_id):
            return False, f"Ключ привязан к другому счету ({db_account_id})"

        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_at:
            return False, "Срок действия подписки истек"

        return True, "Авторизация успешна"
    except Exception as e:
        logger.error(f"Ошибка проверки лицензии в БД: {e}")
        return False, "Внутренняя ошибка сервера при проверке подписки"

# ==========================================
# WEBSOCKET EVENT HANDLERS
# ==========================================
@socketio.on('connect')
def handle_connect():
    logger.info(f"[WS] Клиент подключился (SID: {request.sid}, IP: {request.remote_addr})")

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"[WS] Клиент отключился (SID: {request.sid})")

@socketio.on('join_instrument')
def handle_join_instrument(data):
    """
    Авторизация cBot и вход в комнату XAUUSD
    Ожидаемый JSON: {"symbol": "XAUUSD", "api_key": "...", "account_id": "12345678"}
    """
    data = data or {}
    symbol = data.get('symbol', '').upper()
    api_key = data.get('api_key', '')
    account_id = data.get('account_id', '')

    if symbol != ROOM_NAME:
        emit('response', {'status': 'ERROR', 'message': f'Неверный инструмент: {symbol}. Ожидается {ROOM_NAME}'})
        return

    is_valid, msg = validate_client(api_key, account_id)
    if is_valid:
        join_room(ROOM_NAME)
        logger.info(f"[WS SUCCESS] Клиент {request.sid} (Счет: {account_id}) вошел в комнату {ROOM_NAME}")
        emit('response', {'status': 'SUCCESS', 'message': f'Успешно подключено к комнате {ROOM_NAME}'})
    else:
        logger.warning(f"[WS DENIED] Отклонено для {request.sid} (Счет: {account_id}): {msg}")
        emit('response', {'status': 'ERROR', 'message': f'Ошибка авторизации: {msg}'})

@socketio.on('leave_instrument')
def handle_leave_instrument(data):
    leave_room(ROOM_NAME)
    logger.info(f"[WS] Клиент {request.sid} покинул комнату {ROOM_NAME}")

# ==========================================
# ТЕЛЕГРАМ УВЕДОМЛЕНИЯ
# ==========================================
def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    chat_id = TELEGRAM_CHAT_ID if TELEGRAM_CHAT_ID.startswith("@") or TELEGRAM_CHAT_ID.startswith("-") else f"@{TELEGRAM_CHAT_ID}"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            logger.info(f"[+] Сигнал отправлен в Telegram ({chat_id})")
        else:
            logger.error(f"[-] Ошибка отправки в Telegram ({res.status_code}): {res.text}")
    except Exception as e:
        logger.error(f"[-] Исключение при отправке в Telegram: {e}")

# ==========================================
# ВЫГРУЗКА ДАННЫХ И РАСЧЕТ АНАЛИТИКИ
# ==========================================
def is_market_open():
    """Проверка окна 08:00 - 20:00 (Europe/Chisinau)"""
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
                logger.warning(f"[-] Ошибка TwelveData на {interval} (попытка {attempt}/{retries}): {res.get('message', 'No values')}")
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
            logger.error(f"[-] Исключение при загрузке {interval} (попытка {attempt}/{retries}): {e}")
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

def broadcast_signal(action, entry, sl, tp, reason=""):
    signal_data = {
        "symbol": ROOM_NAME,
        "action": action,
        "entry": float(entry),
        "sl": float(sl),
        "tp": float(tp),
        "reason": reason,
        "timestamp": int(time.time())
    }

    # Мгновенная рассылка в WebSocket-комнату XAUUSD
    socketio.emit('new_signal', signal_data, room=ROOM_NAME)
    logger.info(f"⚡ [WEBSOCKET PUSH -> {ROOM_NAME}] Action: {action} | Entry: {entry} | SL: {sl} | TP: {tp} | Reason: {reason}")

    # Отправка в Telegram
    now_str = datetime.now(ZoneInfo("Europe/Chisinau")).strftime('%H:%M:%S')
    icon = "🟢" if action == "BUY" else ("🔴" if action == "SELL" else "ℹ️")
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
    send_telegram(msg)

def send_test_signal():
    """Тестовый сигнал при запуске сервера для проверки cBot"""
    logger.info("🧪 Генерация тестового WebSocket сигнала при старте...")
    broadcast_signal(
        action="TEST",
        entry=2700.00,
        sl=2690.00,
        tp=2720.00,
        reason="Инициализация сервера / Проверка подключения"
    )

def generate_hourly_signal():
    """Генерация сигнала по multi-TF анализу"""
    try:
        if not is_market_open():
            now_local_str = datetime.now(ZoneInfo("Europe/Chisinau")).strftime('%H:%M:%S')
            logger.info(f"[ℹ️] Вне рабочего окна (08:00 - 20:00). Текущее время: {now_local_str}")
            return

        now_str = datetime.now(ZoneInfo("Europe/Chisinau")).strftime('%H:%M:%S')
        logger.info(f"🔍 [{now_str}] Старт Multi-TF скальп-анализа XAU/USD (M1, M5, M15, M30)...")

        df_m1, df_m5, df_m15, df_m30 = get_multi_tf_market_data()
        if df_m1 is None or df_m5 is None or df_m15 is None or df_m30 is None:
            logger.error("[-] Ошибка: Не все таймфреймы загружены. Пропуск скана.")
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
            broadcast_signal("BUY", curr_price, sl, tp, "M30-Trend + SMC Buy Sweep")
            return

        if float(m1_tail['High'].max()) > local_high and not is_bullish:
            raw_sl_dist = (float(m1_tail['High'].max()) + 1.0) - curr_price
            sl_dist = max(MIN_SL_DIST, min(raw_sl_dist, MAX_SL_DIST, m5_atr * 2.0))
            sl = round(curr_price + sl_dist, 2)
            tp = round(curr_price - (sl_dist * RR_RATIO), 2)
            broadcast_signal("SELL", curr_price, sl, tp, "M30-Trend + SMC Sell Sweep")
            return

        # 3. Breakout M15
        range_max = float(df_m15.tail(4)['High'].max())
        range_min = float(df_m15.tail(4)['Low'].min())

        if curr_price >= range_max - 0.5 and is_bullish:
            raw_sl_dist = curr_price - (range_min - 0.5)
            sl_dist = max(MIN_SL_DIST, min(raw_sl_dist, MAX_SL_DIST))
            sl = round(curr_price - sl_dist, 2)
            tp = round(curr_price + (sl_dist * RR_RATIO), 2)
            broadcast_signal("BUY", curr_price, sl, tp, "M15 Breakout High")
            return
        elif curr_price <= range_min + 0.5 and not is_bullish:
            raw_sl_dist = (range_max + 0.5) - curr_price
            sl_dist = max(MIN_SL_DIST, min(raw_sl_dist, MAX_SL_DIST))
            sl = round(curr_price - sl_dist, 2)
            tp = round(curr_price - (sl_dist * RR_RATIO), 2)
            broadcast_signal("SELL", curr_price, sl, tp, "M15 Breakout Low")
            return

        # 4. Базовый сигнал по тренду
        sl_dist = max(MIN_SL_DIST, m5_atr * 1.8)
        if is_bullish:
            sl = round(curr_price - sl_dist, 2)
            tp = round(curr_price + (sl_dist * RR_RATIO), 2)
            broadcast_signal("BUY", curr_price, sl, tp, "Hourly Trend BUY")
        else:
            sl = round(curr_price + sl_dist, 2)
            tp = round(curr_price - (sl_dist * RR_RATIO), 2)
            broadcast_signal("SELL", curr_price, sl, tp, "Hourly Trend SELL")

    except Exception as e:
        logger.error(f"[-] Исключение при выполнении generate_hourly_signal: {e}")

# ==========================================
# СИНХРОНИЗИРОВАННЫЙ ТАЙМЕР (HH:59:00 UTC)
# ==========================================
def get_seconds_until_next_hour_59():
    """Расчет секунд до ближайшей минуты HH:59:00 UTC"""
    now = datetime.now(timezone.utc)
    if now.minute >= 59:
        target_time = (now + timedelta(hours=1)).replace(minute=59, second=0, microsecond=0)
    else:
        target_time = now.replace(minute=59, second=0, microsecond=0)
    return max((target_time - now).total_seconds(), 5)

def hourly_scheduler_loop():
    time.sleep(3)
    
    # Отправка тестового сигнала при старте
    send_test_signal()

    while True:
        try:
            sleep_time = get_seconds_until_next_hour_59()
            logger.info(f"⏳ Ожидание {round(sleep_time / 60, 1)} мин. ({int(sleep_time)} сек.) до следующего скана (HH:59)...")
            time.sleep(sleep_time)
            generate_hourly_signal()
        except Exception as e:
            logger.error(f"[-] Ошибка в фоновом цикле таймера: {e}")
            time.sleep(10)

# ==========================================
# ЗАПУСК СЕРВЕРА
# ==========================================
if __name__ == '__main__':
    init_db()
    threading.Thread(target=hourly_scheduler_loop, daemon=True).start()
    port = int(os.environ.get('PORT', 5001))
    logger.info(f"🚀 Сервер XAUUSD Scalper запущен на порту {port}...")
    socketio.run(app, host='0.0.0.0', port=port)
