import os
import time
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request
import pandas as pd
import requests

app = Flask(__name__)

# --- НАСТРОЙКИ СИСТЕМЫ ---
TWELVE_DATA_API_KEY = "c997ad22987e477e83034ea132621542"
SYMBOL = "XAU/USD"

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

def is_market_open():
    """Проверка окна 08:00 - 20:00 (Moldova / Europe/Chisinau)"""
    now_local = datetime.now(ZoneInfo("Europe/Chisinau"))
    if now_local.weekday() >= 5:
        return False
    return 8 <= now_local.hour < 20

def get_multi_tf_market_data():
    """Загрузка данных M1, M5, M15 и M30 через Twelve Data API"""
    try:
        dataframes = {}
        intervals = ["1min", "5min", "15min", "30min"]
        
        for interval in intervals:
            url = f"https://api.twelvedata.com/time_series?symbol={SYMBOL}&interval={interval}&outputsize=30&apikey={TWELVE_DATA_API_KEY}"
            res = requests.get(url, timeout=10).json()
            
            if "values" not in res:
                print(f"[-] Ошибка TwelveData API на таймфрейме {interval}: {res.get('message', '')}")
                return None
            
            df = pd.DataFrame(res["values"])
            for col in ['open', 'high', 'low', 'close']:
                df[col] = df[col].astype(float)
            df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
            df.iloc[:] = df.iloc[::-1].values
            dataframes[interval] = df

        return dataframes["1min"], dataframes["5min"], dataframes["15min"], dataframes["30min"]
    except Exception as e:
        print(f"[-] Ошибка загрузки данных: {e}")
        return None, None, None, None

def update_scalp_signal(action, entry, sl, tp, reason=""):
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
    print(f"⚡ [ПОЧАСОВОЙ СИГНАЛ - {reason}] {action} @ {entry} (SL: {sl}, TP: {tp})")

def generate_hourly_signal():
    """Принудительный аналитический скан с учетом M30/M15 тренда и M5/M1 входа"""
    if not is_market_open():
        now_local_str = datetime.now(ZoneInfo("Europe/Chisinau")).strftime('%H:%M:%S')
        print(f"[ℹ️] Вне рабочего окна (08:00 - 20:00). Текущее время: {now_local_str}")
        return

    now_str = datetime.now(ZoneInfo("Europe/Chisinau")).strftime('%H:%M:%S')
    print(f"🔍 [{now_str}] Старт комплексного multi-TF анализа XAU/USD (M1, M5, M15, M30)...")

    df_m1, df_m5, df_m15, df_m30 = get_multi_tf_market_data()
    if df_m1 is None:
        return

    curr_price = round(float(df_m1['Close'].iloc[-1]), 2)

    # --- 1. ОПРЕДЕЛЕНИЕ ТРЕНДА НА M30 И M15 ---
    m30_ema = df_m30['Close'].tail(10).mean()
    m15_ema = df_m15['Close'].tail(10).mean()
    
    # Тренд считаем бычьим (BULLISH), если цена выше EMA M30 и M15
    is_bullish_bias = curr_price >= m30_ema and curr_price >= m15_ema

    # --- 2. SMC LIQUIDITY SWEEP (с учетом контекста) ---
    m5_tail = df_m5.tail(12)
    m1_tail = df_m1.tail(5)
    local_high = float(m5_tail['High'].iloc[:-1].max())
    local_low = float(m5_tail['Low'].iloc[:-1].min())

    if float(m1_tail['Low'].min()) < local_low and is_bullish_bias:
        sl = round(float(m1_tail['Low'].min()) - 0.5, 2)
        risk = max(curr_price - sl, 1.5)
        update_scalp_signal("BUY", curr_price, sl, round(curr_price + risk * 2.0, 2), "M30-Trend + SMC Buy Sweep")
        return

    if float(m1_tail['High'].max()) > local_high and not is_bullish_bias:
        sl = round(float(m1_tail['High'].max()) + 0.5, 2)
        risk = max(sl - curr_price, 1.5)
        update_scalp_signal("SELL", curr_price, sl, round(curr_price - risk * 2.0, 2), "M30-Trend + SMC Sell Sweep")
        return

    # --- 3. BREAKOUT M15/M5 ---
    range_max = float(m15_tail_max := df_m15.tail(4)['High'].max())
    range_min = float(df_m15.tail(4)['Low'].min())

    if curr_price >= range_max - 0.5 and is_bullish_bias:
        sl = round(range_min - 0.5, 2)
        risk = max(curr_price - sl, 1.5)
        update_scalp_signal("BUY", curr_price, sl, round(curr_price + risk * 2.0, 2), "M15 Breakout High")
        return
    elif curr_price <= range_min + 0.5 and not is_bullish_bias:
        sl = round(range_max + 0.5, 2)
        risk = max(sl - curr_price, 1.5)
        update_scalp_signal("SELL", curr_price, sl, round(curr_price - risk * 2.0, 2), "M15 Breakout Low")
        return

    # --- 4. ДЕФОЛТНЫЙ ПОЧАСОВОЙ СИГНАЛ ПО ТРЕНДУ M30/M15 ---
    if is_bullish_bias:
        sl = round(float(m1_tail['Low'].min()) - 0.6, 2)
        risk = max(curr_price - sl, 1.5)
        update_scalp_signal("BUY", curr_price, sl, round(curr_price + risk * 2.0, 2), "M30 Trend Follow BUY")
    else:
        sl = round(float(m1_tail['High'].max()) + 0.6, 2)
        risk = max(sl - curr_price, 1.5)
        update_scalp_signal("SELL", curr_price, sl, round(curr_price - risk * 2.0, 2), "M30 Trend Follow SELL")

# --- ФОНОВЫЙ ПОТОК (Запуск при старте + ровно каждый час) ---
def hourly_scheduler_loop():
    print("[+] [SCALPER] Почасовой multi-TF таймер запущен!")
    generate_hourly_signal()

    while True:
        now = datetime.now()
        seconds_until_next_hour = 3600 - (now.minute * 60 + now.second)
        time.sleep(seconds_until_next_hour)
        generate_hourly_signal()

threading.Thread(target=hourly_scheduler_loop, daemon=True).start()

# --- REST API ---

@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "running", "bot": "XAUUSD Multi-TF Hourly Scalper Engine"})

@app.route('/scalp_signal', methods=['GET'])
def get_scalp_signal():
    with lock:
        return jsonify(scalp_signal)

@app.route('/scalp_ack', methods=['POST'])
def acknowledge_scalp_signal():
    global scalp_signal
    with lock:
        scalp_signal["status"] = "NONE"
        scalp_signal["action"] = "NONE"
        print("👍 [ACK] Сигнал принят cTrader.")
        return jsonify({"status": "acknowledged"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
