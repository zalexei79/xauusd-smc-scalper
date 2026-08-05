import os
import time
import threading
from datetime import datetime, timezone
from flask import Flask, jsonify, request
import pandas as pd
import requests

app = Flask(__name__)

# --- НАСТРОЙКИ СИСТЕМЫ ---
TWELVE_DATA_API_KEY = "c997ad22987e477e83034ea132621542"
SYMBOL = "XAU/USD"

# Глобальное состояние сигнала скальпера для cTrader
scalp_signal = {
    "symbol": "XAUUSD",
    "action": "NONE",
    "entry": 0.0,
    "sl": 0.0,
    "tp": 0.0,
    "status": "NONE",
    "timestamp": 0
}

lock = threading.Lock()
last_analyzed_candle = None

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_market_open():
    """Проверяет торговое окно (Пн-Пт, с 08:00 до 20:00 UTC)"""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return 8 <= now.hour < 20

def get_scalp_market_data():
    """Загрузка данных M1 и M5 через Twelve Data API"""
    try:
        url_m1 = f"https://api.twelvedata.com/time_series?symbol={SYMBOL}&interval=1min&outputsize=30&apikey={TWELVE_DATA_API_KEY}"
        url_m5 = f"https://api.twelvedata.com/time_series?symbol={SYMBOL}&interval=5min&outputsize=30&apikey={TWELVE_DATA_API_KEY}"

        res_m1 = requests.get(url_m1, timeout=10).json()
        res_m5 = requests.get(url_m5, timeout=10).json()

        if "values" not in res_m1 or "values" not in res_m5:
            err_m1 = res_m1.get('message', '')
            err_m5 = res_m5.get('message', '')
            print(f"[-] Ошибка TwelveData API: {err_m1 or err_m5}")
            return None, None

        df_m1 = pd.DataFrame(res_m1["values"])
        df_m5 = pd.DataFrame(res_m5["values"])

        for df in [df_m1, df_m5]:
            for col in ['open', 'high', 'low', 'close']:
                df[col] = df[col].astype(float)
            df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
            # Разворачиваем хронологию (от старых к новым)
            df.iloc[:] = df.iloc[::-1].values

        return df_m5, df_m1
    except Exception as e:
        print(f"[-] Ошибка загрузки TwelveData: {e}")
        return None, None

def update_scalp_signal(action, entry, sl, tp, reason=""):
    """Обновление состояния API для cBot"""
    global scalp_signal
    with lock:
        scalp_signal = {
            "symbol": "XAUUSD",
            "action": action,
            "entry": float(entry),
            "sl": float(sl),
            "tp": float(tp),
            "status": "NEW",
            "timestamp": int(time.time())
        }
    print(f"⚡ [СИГНАЛ НАЙДЕН - {reason}] {action} @ {entry} (SL: {sl}, TP: {tp})")

# --- КОМПЛЕКСНЫЙ АНАЛИЗАТОР (SMC + Уровни + Пробои + FVG) ---
def run_full_market_analysis():
    global last_analyzed_candle

    if not is_market_open():
        print("[ℹ️] Рынок закрыт или вне рабочего окна (08:00 - 20:00 UTC).")
        return

    # Если текущий сигнал еще не обработан cTrader — ждем
    with lock:
        if scalp_signal["status"] == "NEW":
            return

    df_m5, df_m1 = get_scalp_market_data()
    if df_m5 is None or df_m1 is None:
        return

    # Проверка на повторный анализ той же M5 свечи
    latest_candle_time = df_m5.iloc[-1].get('datetime', str(time.time()))
    if last_analyzed_candle == latest_candle_time:
        return

    last_analyzed_candle = latest_candle_time
    print(f"🔍 [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] Старт анализа XAU/USD (Twelve Data)...")

    curr_price = round(float(df_m1['Close'].iloc[-1]), 2)

    # -------------------------------------------------------------
    # СТРАТЕГИЯ 1: SMC Liquidity Sweep (Снятие ликвидности)
    # -------------------------------------------------------------
    m5_window = df_m5.tail(12)
    local_high = float(m5_window['High'].iloc[:-1].max())
    local_low = float(m5_window['Low'].iloc[:-1].min())
    m1_tail = df_m1.tail(3)

    if float(m1_tail['Low'].min()) < local_low and curr_price > local_low:
        sl = round(float(m1_tail['Low'].min()) - 0.6, 2)
        risk = curr_price - sl
        if 1.0 <= risk <= 6.0:
            update_scalp_signal("BUY", curr_price, sl, round(curr_price + risk * 2.0, 2), "SMC Sweep Buy")
            return

    if float(m1_tail['High'].max()) > local_high and curr_price < local_high:
        sl = round(float(m1_tail['High'].max()) + 0.6, 2)
        risk = sl - curr_price
        if 1.0 <= risk <= 6.0:
            update_scalp_signal("SELL", curr_price, sl, round(curr_price - risk * 2.0, 2), "SMC Sweep Sell")
            return

    # -------------------------------------------------------------
    # СТРАТЕГИЯ 2: Пробой Накопления / Консолидации (Breakout M5)
    # -------------------------------------------------------------
    range_window = df_m5.tail(6)  # Последние 30 минут
    range_max = float(range_window['High'].max())
    range_min = float(range_window['Low'].min())
    range_size = range_max - range_min

    if range_size <= 3.5:  # Узкое накопление ($3.50)
        if curr_price > range_max:
            sl = round(range_min - 0.5, 2)
            risk = curr_price - sl
            if 1.0 <= risk <= 6.0:
                update_scalp_signal("BUY", curr_price, sl, round(curr_price + risk * 2.0, 2), "Breakout Range Buy")
                return
        elif curr_price < range_min:
            sl = round(range_max + 0.5, 2)
            risk = sl - curr_price
            if 1.0 <= risk <= 6.0:
                update_scalp_signal("SELL", curr_price, sl, round(curr_price - risk * 2.0, 2), "Breakout Range Sell")
                return

    # -------------------------------------------------------------
    # СТРАТЕГИЯ 3: Дисбаланс / Импульс M1 (FVG Scalp)
    # -------------------------------------------------------------
    c1, c2, c3 = df_m1.iloc[-3], df_m1.iloc[-2], df_m1.iloc[-1]

    # Bullish FVG
    if float(c3['Low']) > float(c1['High']) and (float(c2['Close']) - float(c2['Open'])) > 1.2:
        sl = round(float(c2['Low']) - 0.5, 2)
        risk = curr_price - sl
        if 1.0 <= risk <= 6.0:
            update_scalp_signal("BUY", curr_price, sl, round(curr_price + risk * 2.0, 2), "Impulse FVG Buy")
            return

    # Bearish FVG
    if float(c3['High']) < float(c1['Low']) and (float(c2['Open']) - float(c2['Close'])) > 1.2:
        sl = round(float(c2['High']) + 0.5, 2)
        risk = sl - curr_price
        if 1.0 <= risk <= 6.0:
            update_scalp_signal("SELL", curr_price, sl, round(curr_price - risk * 2.0, 2), "Impulse FVG Sell")
            return

# --- ФОНОВЫЙ ПОТОК (Сканирование раз в 60 сек) ---
def scalp_scanner_loop():
    print("[+] [SCALPER] Фоновый движок анализа (Twelve Data) запущен!")

    # Мгновенный первичный анализ при запуске
    run_full_market_analysis()

    while True:
        try:
            run_full_market_analysis()
            time.sleep(60)  # Запрос раз в 1 минуту (безопасно для лимитов TwelveData)
        except Exception as e:
            print(f"[-] Ошибка в цикл-сканере: {e}")
            time.sleep(10)

threading.Thread(target=scalp_scanner_loop, daemon=True).start()

# --- HTTP ENDPOINTS (REST API) ---

@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "running", "bot": "XAUUSD SMC Scalper Engine (Twelve Data)"})

@app.route('/scalp_signal', methods=['GET'])
def get_scalp_signal():
    with lock:
        return jsonify(scalp_signal)

@app.route('/scalp_ack', methods=['POST'])
def acknowledge_scalp_signal():
    global scalp_signal
    data = request.get_json(silent=True) or {}
    ts = data.get('timestamp')
    with lock:
        scalp_signal["status"] = "NONE"
        scalp_signal["action"] = "NONE"
        print(f"👍 [ACK] Сигнал (ts={ts}) успешно обработан cTrader.")
        return jsonify({"status": "acknowledged"})

@app.route('/test_scalp', methods=['GET', 'POST'])
def trigger_test_scalp():
    """Принудительный запуск анализа или генерация тестового сигнала"""
    run_full_market_analysis()
    return jsonify({"message": "Принудительный анализ выполнен!", "signal": scalp_signal}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
