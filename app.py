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
last_analyzed_candle = None

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_market_open():
    """Проверяет торговое окно (Пн-Пт, с 08:00 до 20:00 UTC)"""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    return 8 <= now.hour < 20

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
        print(f"[-] Ошибка загрузки данных Yahoo: {e}")
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

# --- КОМПЛЕКСНЫЙ АНАЛИЗАТОР (SMC + Уровни + Пробои + Накопления) ---
def run_full_market_analysis():
    global last_analyzed_candle
    
    if not is_market_open():
        print("[ℹ️] Рынок закрыт или вне рабочего окна (08:00 - 20:00 UTC).")
        return

    # Если сигнал еще не забран cTrader, не перетираем его
    with lock:
        if scalp_signal["status"] == "NEW":
            return

    df_m5, df_m1 = get_scalp_market_data()
    if df_m5 is None or df_m1 is None:
        return

    # Проверка, чтобы не анализировать одну и ту же M5 свечу несколько раз
    latest_candle_time = df_m5.index[-1]
    if last_analyzed_candle == latest_candle_time:
        return
    
    last_analyzed_candle = latest_candle_time
    print(f"🔍 [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] Старт анализа M1/M5...")

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
    range_window = df_m5.tail(6) # Последние 30 минут
    range_max = range_window['High'].max()
    range_min = range_window['Low'].min()
    range_size = range_max - range_min

    # Если было узкое накопление (флэт менее $3)
    if range_size <= 3.5:
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
    # СТРАТЕГИЯ 3: Дисбаланс / Имппульс M1 (FVG Scalp)
    # -------------------------------------------------------------
    c1, c2, c3 = df_m1.iloc[-3], df_m1.iloc[-2], df_m1.iloc[-1]
    
    # Bullish FVG
    if c3['Low'] > c1['High'] and (c2['Close'] - c2['Open']) > 1.2:
        sl = round(c2['Low'] - 0.5, 2)
        risk = curr_price - sl
        if 1.0 <= risk <= 6.0:
            update_scalp_signal("BUY", curr_price, sl, round(curr_price + risk * 2.0, 2), "Impulse FVG Buy")
            return

    # Bearish FVG
    if c3['High'] < c1['Low'] and (c2['Open'] - c2['Close']) > 1.2:
        sl = round(c2['High'] + 0.5, 2)
        risk = sl - curr_price
        if 1.0 <= risk <= 6.0:
            update_scalp_signal("SELL", curr_price, sl, round(curr_price - risk * 2.0, 2), "Impulse FVG Sell")
            return

# --- ФОНОВЫЙ ПОТОК (Запуск при старте + каждые 60 секунд) ---
def scalp_scanner_loop():
    print("[+] [SCALPER] Фоновый движок анализа запущен!")
    
    # Первичный анализ прямо при старте сервера
    run_full_market_analysis()
    
    while True:
        try:
            run_full_market_analysis()
            time.sleep(60) # Сканирование раз в 1 минуту
        except Exception as e:
            print(f"[-] Ошибка в цикл-сканере: {e}")
            time.sleep(10)

threading.Thread(target=scalp_scanner_loop, daemon=True).start()

# --- HTTP ENDPOINTS (REST API) ---

@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "running", "bot": "XAUUSD SMC Scalper Engine"})

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
    """Мгновенный принудительный анализ или тестовый сигнал"""
    run_full_market_analysis()
    return jsonify({"message": "Принудительный анализ выполнен!", "signal": scalp_signal}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
