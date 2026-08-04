import os
import time
import threading
from datetime import datetime, timezone
from flask import Flask, jsonify, request
import pandas as pd
import numpy as np
import yfinance as yf

app = Flask(__name__)

# --- НАСТРОЙКИ СИСТЕМЫ ---
YAHOO_TICKER = "GC=F"
FUTURES_SPOT_OFFSET = 5.4

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

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_market_open():
    """Проверяет, открыт ли рынок (Пн-Пт)."""
    now = datetime.now(timezone.utc)
    return now.weekday() < 5

def get_scalp_market_data():
    """Загрузка данных M5 и M1 с таймаутом"""
    try:
        gold = yf.Ticker(YAHOO_TICKER)
        df_m5 = gold.history(period="1d", interval="5m", timeout=10)
        df_m1 = gold.history(period="1d", interval="1m", timeout=10)
        
        if df_m5.empty or df_m1.empty:
            return None, None
            
        for df in [df_m5, df_m1]:
            df['High'] -= FUTURES_SPOT_OFFSET
            df['Low'] -= FUTURES_SPOT_OFFSET
            df['Close'] -= FUTURES_SPOT_OFFSET
            df['Open'] -= FUTURES_SPOT_OFFSET
            
        return df_m5, df_m1
    except Exception as e:
        print(f"[-] Ошибка загрузки данных M1/M5 Yahoo: {e}")
        return None, None

def update_scalp_signal(action, entry, sl, tp):
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
    print(f"⚡ [SCALPER M1/M5] Новый сигнал для cTrader: {action} @ {entry} (SL: {sl}, TP: {tp})")

# --- АНАЛИЗАТОР СКАЛЬПИНГА (БОЛЕЕ АГРЕССИВНЫЙ M1/M5 SMC) ---
def analyze_scalper_setup():
    if not is_market_open():
        return

    try:
        df_m5, df_m1 = get_scalp_market_data()
        if df_m5 is None or df_m1 is None:
            return

        # Исключаем повторную генерацию, если текущий сигнал ещё не обработан cTrader
        with lock:
            if scalp_signal["status"] == "NEW":
                return

        # 1. Определение локальных уровней M5 (за последние 10 свечей)
        m5_window = df_m5.tail(10)
        local_high = float(m5_window['High'].iloc[:-1].max())
        local_low = float(m5_window['Low'].iloc[:-1].min())
        
        m1_tail = df_m1.tail(4)
        last_m1 = m1_tail.iloc[-1]
        recent_low = float(m1_tail['Low'].min())
        recent_high = float(m1_tail['High'].max())
        
        # BUY Scalp Setup (Sweep снизу)
        # Проверяем: минимальная цена за последние свечи пробивала local_low, но закрытие последней M1 выше
        if recent_low < local_low and last_m1['Close'] > local_low:
            entry = round(float(last_m1['Close']), 2)
            sl = round(recent_low - 0.5, 2)
            risk = entry - sl
            
            # Более гибкий диапазон риска ($1.0 - $6.0) и R:R 1:2
            if 1.0 <= risk <= 6.0:
                tp = round(entry + (risk * 2.0), 2)
                update_scalp_signal("BUY", entry, sl, tp)

        # SELL Scalp Setup (Sweep сверху)
        # Проверяем: максимальная цена за последние свечи пробивала local_high, но закрытие последней M1 ниже
        elif recent_high > local_high and last_m1['Close'] < local_high:
            entry = round(float(last_m1['Close']), 2)
            sl = round(recent_high + 0.5, 2)
            risk = sl - entry
            
            # Более гибкий диапазон риска ($1.0 - $6.0) и R:R 1:2
            if 1.0 <= risk <= 6.0:
                tp = round(entry - (risk * 2.0), 2)
                update_scalp_signal("SELL", entry, sl, tp)

    except Exception as e:
        print(f"[-] Ошибка скальпер-анализатора: {e}")

# --- ФОНОВЫЙ ПОТОК СКАНИРОВАНИЯ ---
def scalp_scanner_loop():
    print("[+] [SCALPER] Сканер M1/M5 успешно запущен в фоновом режиме!")
    while True:
        try:
            analyze_scalper_setup()
            time.sleep(20)  # Ускоренное сканирование (раз в 20 секунд)
        except Exception as e:
            print(f"[-] Ошибка в scalp_scanner_loop: {e}")
            time.sleep(10)

threading.Thread(target=scalp_scanner_loop, daemon=True).start()

# --- HTTP ENDPOINTS (REST API) ---

@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "running", "bot": "XAUUSD SMC Scalper Engine"})

@app.route('/scalp_signal', methods=['GET'])
def get_scalp_signal():
    """Опрос бота с cTrader"""
    with lock:
        return jsonify(scalp_signal)

@app.route('/scalp_ack', methods=['POST'])
def acknowledge_scalp_signal():
    """Подтверждение от cTrader и сброс состояния"""
    global scalp_signal
    data = request.get_json(silent=True) or {}
    ts = data.get('timestamp')
    with lock:
        scalp_signal["status"] = "NONE"
        scalp_signal["action"] = "NONE"
        print(f"👍 [SCALPER ACK] Сигнал (ts={ts}) принят cTrader и сброшен в NONE.")
        return jsonify({"status": "acknowledged"})

@app.route('/test_scalp', methods=['GET', 'POST'])
def trigger_test_scalp():
    """Тестовый вызов для быстрой проверки исполнения в cTrader"""
    update_scalp_signal("BUY", 2650.00, 2647.50, 2657.50)
    return jsonify({"message": "Тестовый сигнал создан!", "signal": scalp_signal}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
