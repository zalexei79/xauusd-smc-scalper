import os
import time
import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request
import pandas as pd
import requests

app = Flask(__name__)

# --- НАСТРОЙКИ СИСТЕМЫ ---
TWELVE_DATA_API_KEY = "c997ad22987e477e83034ea132621542"
SYMBOL = "XAU/USD"
SIGNAL_FILE = "/tmp/scalp_signal.json"

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

def get_batch_market_data():
    """Загрузка M1, M5, M15, M30 за 1 быстрой запрос"""
    try:
        url = f"https://api.twelvedata.com/time_series?symbol={SYMBOL}&interval=1min,5min,15min,30min&outputsize=30&apikey={TWELVE_DATA_API_KEY}"
        res = requests.get(url, timeout=12).json()

        dfs = {}
        for tf in ["1min", "5min", "15min", "30min"]:
            tf_data = res.get(tf, {}) if isinstance(res, dict) else {}
            if "values" not in tf_data:
                print(f"[-] Ошибка получения данных для {tf}")
                return None, None, None, None
            
            df = pd.DataFrame(tf_data["values"])
            for col in ['open', 'high', 'low', 'close']:
                df[col] = df[col].astype(float)
            df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
            df.iloc[:] = df.iloc[::-1].values
            dfs[tf] = df

        return dfs["1min"], dfs["5min"], dfs["15min"], dfs["30min"]
    except Exception as e:
        print(f"[-] Ошибка загрузки Batch Data: {e}")
        return None, None, None, None

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
    print(f"⚡ [ПОЧАСОВОЙ СИГНАЛ - {reason}] {action} @ {entry} (SL: {sl}, TP: {tp})")

def generate_hourly_signal():
    """Генерация лучшего сигнала на текущий час"""
    if not is_market_open():
        now_local_str = datetime.now(ZoneInfo("Europe/Chisinau")).strftime('%H:%M:%S')
        print(f"[ℹ️] Вне рабочего окна (08:00 - 20:00). Текущее время: {now_local_str}")
        return

    now_str = datetime.now(ZoneInfo("Europe/Chisinau")).strftime('%H:%M:%S')
    print(f"🔍 [{now_str}] Старт Multi-TF анализа XAU/USD (Batch API)...")

    df_m1, df_m5, df_m15, df_m30 = get_batch_market_data()
    if df_m1 is None:
        print("[-] Не удалось получить данные свечей.")
        return

    curr_price = round(float(df_m1['Close'].iloc[-1]), 2)

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
        sl = round(float(m1_tail['Low'].min()) - 0.5, 2)
        risk = max(curr_price - sl, 1.5)
        update_scalp_signal("BUY", curr_price, sl, round(curr_price + risk * 2.0, 2), "M30-Trend + SMC Buy Sweep")
        return

    if float(m1_tail['High'].max()) > local_high and not is_bullish:
        sl = round(float(m1_tail['High'].max()) + 0.5, 2)
        risk = max(sl - curr_price, 1.5)
        update_scalp_signal("SELL", curr_price, sl, round(curr_price - risk * 2.0, 2), "M30-Trend + SMC Sell Sweep")
        return

    # 3. Breakout M15
    range_max = float(df_m15.tail(4)['High'].max())
    range_min = float(df_m15.tail(4)['Low'].min())

    if curr_price >= range_max - 0.5 and is_bullish:
        sl = round(range_min - 0.5, 2)
        risk = max(curr_price - sl, 1.5)
        update_scalp_signal("BUY", curr_price, sl, round(curr_price + risk * 2.0, 2), "M15 Breakout High")
        return
    elif curr_price <= range_min + 0.5 and not is_bullish:
        sl = round(range_max + 0.5, 2)
        risk = max(sl - curr_price, 1.5)
        update_scalp_signal("SELL", curr_price, sl, round(curr_price - risk * 2.0, 2), "M15 Breakout Low")
        return

    # 4. Базовый сигнал по тренду
    if is_bullish:
        sl = round(float(m1_tail['Low'].min()) - 0.6, 2)
        risk = max(curr_price - sl, 1.5)
        update_scalp_signal("BUY", curr_price, sl, round(curr_price + risk * 2.0, 2), "Hourly Trend BUY")
    else:
        sl = round(float(m1_tail['High'].max()) + 0.6, 2)
        risk = max(sl - curr_price, 1.5)
        update_scalp_signal("SELL", curr_price, sl, round(curr_price - risk * 2.0, 2), "Hourly Trend SELL")

def hourly_scheduler_loop():
    time.sleep(3)
    generate_hourly_signal()

    while True:
        now = datetime.now()
        seconds_until_next_hour = 3600 - (now.minute * 60 + now.second)
        time.sleep(seconds_until_next_hour)
        generate_hourly_signal()

threading.Thread(target=hourly_scheduler_loop, daemon=True).start()

# --- REST API ENDPOINTS ---

@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "running", "bot": "XAUUSD Hourly Scalper Engine"})

@app.route('/scalp_signal', methods=['GET'])
def get_scalp_signal():
    with lock:
        signal = load_signal_from_file()
        return jsonify(signal)

@app.route('/scalp_ack', methods=['POST'])
def acknowledge_scalp_signal():
    with lock:
        save_signal_to_file(EMPTY_SIGNAL)
        print("👍 [ACK] Сигнал сброшен после обработки cTrader.")
        return jsonify({"status": "acknowledged"})

@app.route('/force_signal', methods=['GET', 'POST'])
def force_signal():
    generate_hourly_signal()
    with lock:
        signal = load_signal_from_file()
        return jsonify({"message": "Принудительный сигнал сгенерирован", "signal": signal})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
