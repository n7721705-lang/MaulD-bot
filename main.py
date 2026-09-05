import asyncio
import time
import aiohttp
from datetime import datetime, timezone, timedelta
from telegram import Bot

# ==================== НАЛАШТУВАННЯ ====================
TELEGRAM_BOT_TOKEN = "8818473462:AAG02pUpdJn0FsBzJabEVdOW7-UrFmMbx4w"
TELEGRAM_CHAT_ID = -1003933274705

# Таймфрейми
TIMEFRAME_REFERENCE = "1h"      # Опорний таймфрейм (Higher Timeframe)
TIMEFRAME_EXECUTION = "5m"      # Таймфрейм для входу (Lower Timeframe)
LOOKBACK_CANDLES = 30           # Кількість свічок для аналізу

# CRT параметри
MIN_WICK_PERCENT = 20.0         # Мінімальна тінь для маніпуляції (% від тіла)
SWEEP_DEPTH = 0.05              # Глибина вимітання ліквідності (5% від ATR)

# Ризик-менеджмент
SL_BUFFER = 0.02                # 2% запас для стопу
TP_RATIO = 1.5                  # Risk/Reward 1:1.5

CHECK_INTERVAL = 300            # Перевірка кожні 5 хвилин
# =====================================================

KYIV_TZ = timezone(timedelta(hours=3))
bot = Bot(token=TELEGRAM_BOT_TOKEN)

tracked_setups = {}
alerted = set()

def get_kyiv_time():
    return datetime.now(KYIV_TZ).strftime('%H:%M:%S')

def format_price(price):
    if price >= 1:
        return f"{price:.4f}"
    elif price >= 0.01:
        return f"{price:.6f}"
    else:
        return f"{price:.8f}"

async def get_klines(symbol, interval, limit=50):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=15) as resp:
                data = await resp.json()
                if not data:
                    return None
                times = [datetime.fromtimestamp(int(k[0])/1000) for k in data]
                opens = [float(k[1]) for k in data]
                highs = [float(k[2]) for k in data]
                lows = [float(k[3]) for k in data]
                closes = [float(k[4]) for k in data]
                return {'times': times, 'opens': opens, 'highs': highs, 'lows': lows, 'closes': closes}
        except Exception as e:
            print(f"❌ Помилка {symbol}: {e}")
            return None

def find_reference_candle(klines):
    """Знаходить опорну свічку (Reference Candle) — найбільша за діапазоном"""
    if len(klines) < 2:
        return None
    
    # Шукаємо свічку з найбільшим діапазоном серед останніх 10
    max_range = 0
    ref_index = -1
    
    for i in range(-10, -1):
        if i >= len(klines):
            continue
        high = klines['highs'][i]
        low = klines['lows'][i]
        candle_range = high - low
        if candle_range > max_range:
            max_range = candle_range
            ref_index = i
    
    if ref_index == -1:
        return None
    
    return {
        'index': ref_index,
        'high': klines['highs'][ref_index],
        'low': klines['lows'][ref_index],
        'open': klines['opens'][ref_index],
        'close': klines['closes'][ref_index],
        'range': max_range,
        'is_bullish': klines['closes'][ref_index] > klines['opens'][ref_index]
    }

def detect_manipulation(klines, ref_candle):
    """Виявляє маніпуляційну свічку (Sweep & Flip)"""
    if not ref_candle or len(klines) < 3:
        return None
    
    # Беремо останню свічку для перевірки
    last = {
        'high': klines['highs'][-1],
        'low': klines['lows'][-1],
        'open': klines['opens'][-1],
        'close': klines['closes'][-1]
    }
    
    # Перевіряємо PUMP (булліш) — вимітання ліквідності вниз
    is_bullish_crt = False
    is_bearish_crt = False
    
    # Булліш CRT: ціна вимітає ліквідність нижче Low, потім закривається всередині
    if last['low'] < ref_candle['low'] and last['close'] > ref_candle['low']:
        # Перевіряємо тінь (маніпуляційна свічка)
        if last['close'] > last['open']:  # Бича свічка
            is_bullish_crt = True
    
    # Ведмежий CRT: ціна вимітає ліквідність вище High, потім закривається всередині
    if last['high'] > ref_candle['high'] and last['close'] < ref_candle['high']:
        if last['close'] < last['open']:  # Ведмежа свічка
            is_bearish_crt = True
    
    if not is_bullish_crt and not is_bearish_crt:
        return None
    
    return {
        'type': 'BULLISH' if is_bullish_crt else 'BEARISH',
        'sweep_price': ref_candle['low'] if is_bullish_crt else ref_candle['high'],
        'reclaim_price': ref_candle['low'] if is_bullish_crt else ref_candle['high'],
        'entry': klines['closes'][-1],
        'ref_candle': ref_candle
    }

def calculate_atr(klines, period=14):
    """Розраховує ATR"""
    if len(klines) < period + 1:
        return 0
    
    tr_values = []
    for i in range(len(klines) - period, len(klines)):
        high = klines['highs'][i]
        low = klines['lows'][i]
        prev_close = klines['closes'][i-1] if i > 0 else high
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
    
    return sum(tr_values) / len(tr_values)

async def send_crt_signal(symbol, setup, atr):
    """Надсилає CRT сигнал у Telegram"""
    emoji = "🟢" if setup['type'] == 'BULLISH' else "🔴"
    direction = "БУЛЛІШ" if setup['type'] == 'BULLISH' else "ВЕДМЕЖИЙ"
    direction_emoji = "📈" if setup['type'] == 'BULLISH' else "📉"
    
    # Розраховуємо рівні
    ref = setup['ref_candle']
    entry = setup['entry']
    
    if setup['type'] == 'BULLISH':
        # Лонг: вхід після reclaim
        stop_loss = entry * (1 - SL_BUFFER - atr/entry * 0.5)
        take_profit = entry + (entry - stop_loss) * TP_RATIO
    else:
        # Шорт
        stop_loss = entry * (1 + SL_BUFFER + atr/entry * 0.5)
        take_profit = entry - (stop_loss - entry) * TP_RATIO
    
    coin_name = symbol.replace('USDT', '')
    
    message = (
        f"{emoji} *{symbol}* ({coin_name}) — CRT СИГНАЛ {direction_emoji}\n"
        f"\n"
        f"📊 *Напрямок:* {direction} {direction_emoji}\n"
        f"🕯 *Опорна свічка (Reference Candle):*\n"
        f"   📈 High: {format_price(ref['high'])} USDT\n"
        f"   📉 Low: {format_price(ref['low'])} USDT\n"
        f"   📊 Діапазон: {format_price(ref['range'])} USDT\n"
        f"\n"
        f"🎯 *Вхід:* {format_price(entry)} USDT\n"
        f"🛑 *Stop Loss:* {format_price(stop_loss)} USDT\n"
        f"🏁 *Take Profit:* {format_price(take_profit)} USDT\n"
        f"📊 *Risk/Reward:* 1:{TP_RATIO:.1f}\n"
        f"\n"
        f"🕐 *Час:* {get_kyiv_time()}"
    )
    
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
        print(f"✅ CRT СИГНАЛ: {symbol} {direction}")
    except Exception as e:
        print(f"❌ Помилка відправки: {e}")

async def scan_crt_setups():
    """Сканує всі монети на наявність CRT сигналів"""
    print(f"🔍 Сканування CRT... {get_kyiv_time()}")
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=15) as resp:
                tickers = await resp.json()
                
                # Фільтруємо монети з об'ємом
                symbols = []
                for item in tickers:
                    symbol = item.get('symbol', '')
                    if symbol.endswith('USDT') and symbol != 'BTCUSDT':
                        volume = float(item.get('quoteVolume', 0))
                        if volume > 5_000_000:  # > $5M об'єму
                            symbols.append(symbol)
                
                print(f"📊 Знайдено {len(symbols)} монет для аналізу")
                
                for symbol in symbols[:15]:  # Для тесту
                    if symbol in alerted:
                        continue
                    
                    # Отримуємо свічки на Higher Timeframe (опорний)
                    klines_htf = await get_klines(symbol, TIMEFRAME_REFERENCE, LOOKBACK_CANDLES)
                    if not klines_htf:
                        continue
                    
                    # Отримуємо свічки на Lower Timeframe (виконання)
                    klines_ltf = await get_klines(symbol, TIMEFRAME_EXECUTION, 30)
                    if not klines_ltf:
                        continue
                    
                    # Знаходимо опорну свічку
                    ref_candle = find_reference_candle(klines_htf)
                    if not ref_candle:
                        continue
                    
                    # Шукаємо маніпуляцію на LTF
                    setup = detect_manipulation(klines_ltf, ref_candle)
                    if not setup:
                        continue
                    
                    # Розраховуємо ATR для SL
                    atr = calculate_atr(klines_ltf)
                    if atr == 0:
                        continue
                    
                    # Надсилаємо сигнал
                    await send_crt_signal(symbol, setup, atr)
                    alerted.add(symbol)
                    await asyncio.sleep(1)
                    
        except Exception as e:
            print(f"❌ Помилка сканування: {e}")

async def main():
    print("=" * 50)
    print("🚀 CRT BOT — CANDLE RANGE THEORY STRATEGY")
    print("=" * 50)
    print(f"📊 Опорний таймфрейм: {TIMEFRAME_REFERENCE}")
    print(f"📈 Таймфрейм виконання: {TIMEFRAME_EXECUTION}")
    print(f"🎯 Risk/Reward: 1:{TP_RATIO}")
    print(f"🛑 Стоп: {SL_BUFFER*100:.0f}% + ATR")
    print(f"🔄 Перевірка кожні {CHECK_INTERVAL//60} хв")
    print("=" * 50)
    
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"✅ *CRT BOT запущено!*\n"
                 f"📊 Опорний таймфрейм: {TIMEFRAME_REFERENCE}\n"
                 f"📈 Таймфрейм виконання: {TIMEFRAME_EXECUTION}\n"
                 f"🎯 Risk/Reward: 1:{TP_RATIO}\n"
                 f"🕐 Київ: {get_kyiv_time()}",
            parse_mode="Markdown"
        )
        print("✅ Бот запущено!")
    except Exception as e:
        print(f"⚠️ Помилка: {e}")
    
    while True:
        try:
            await scan_crt_setups()
            await asyncio.sleep(CHECK_INTERVAL)
        except Exception as e:
            print(f"❌ Помилка в циклі: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
