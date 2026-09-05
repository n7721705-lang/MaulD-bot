import asyncio
import time
import aiohttp
from datetime import datetime, timezone, timedelta
from telegram import Bot

# ==================== НАЛАШТУВАННЯ ====================
TELEGRAM_BOT_TOKEN = "8818473462:AAG02pUpdJn0FsBzJabEVdOW7-UrFmMbx4w"
TELEGRAM_CHAT_ID = -1003933274705

PUMP_THRESHOLD = 7.0          # Мінімальний рух (%)
TIMEFRAME_MAIN = "15m"        # Основний таймфрейм
TIMEFRAME_BOS = ["5m", "3m"]  # Таймфрейми для BOS
FIB_LEVEL = 0.618             # Рівень входу
CHECK_INTERVAL = 60           # Перевірка кожні 60 секунд
LOOKBACK = 30                 # Свічок для аналізу
# =====================================================

KYIV_TZ = timezone(timedelta(hours=3))
bot = Bot(token=TELEGRAM_BOT_TOKEN)

tracked_moves = {}
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

def find_swings(highs, lows, lookback=3):
    """Знаходить HH, HL, LH, LL"""
    swings_high = []  # HH
    swings_low = []   # LL
    
    for i in range(lookback, len(highs) - lookback):
        # Перевіряємо HH (вищий максимум)
        if all(highs[i] > highs[i-j] for j in range(1, lookback+1)) and \
           all(highs[i] > highs[i+j] for j in range(1, lookback+1)):
            swings_high.append({'index': i, 'price': highs[i], 'type': 'HH'})
        
        # Перевіряємо LL (нижчий мінімум)
        if all(lows[i] < lows[i-j] for j in range(1, lookback+1)) and \
           all(lows[i] < lows[i+j] for j in range(1, lookback+1)):
            swings_low.append({'index': i, 'price': lows[i], 'type': 'LL'})
    
    return swings_high, swings_low

def detect_bos(swings_high, swings_low, current_high, current_low):
    """Визначає BOS (злам структури)"""
    bos_signals = []
    
    # BOS вгору: пробій попереднього HH
    if len(swings_high) >= 2:
        last_hh = swings_high[-2]['price']
        if current_high > last_hh:
            bos_signals.append({
                'type': 'BOS_UP',
                'level': last_hh,
                'label': '🚀 BOS ВГОРУ'
            })
    
    # BOS вниз: пробій попереднього LL
    if len(swings_low) >= 2:
        last_ll = swings_low[-2]['price']
        if current_low < last_ll:
            bos_signals.append({
                'type': 'BOS_DOWN',
                'level': last_ll,
                'label': '🔻 BOS ВНИЗ'
            })
    
    return bos_signals

def analyze_structure(klines):
    """Повний аналіз структури"""
    highs = klines['highs']
    lows = klines['lows']
    closes = klines['closes']
    
    swings_high, swings_low = find_swings(highs, lows)
    bos = detect_bos(swings_high, swings_low, highs[-1], lows[-1])
    
    return {
        'swings_high': swings_high,
        'swings_low': swings_low,
        'bos': bos
    }

async def send_signal(symbol, move, entry, sl, tp, start_price, current_price, elapsed, fib_levels, structure, high, low, bos_tf):
    emoji = "🟢" if move > 0 else "🔴"
    action = "прибавила" if move > 0 else "упала"
    change_text = f"+{move:.2f}%" if move > 0 else f"{move:.2f}%"
    coin_name = symbol.replace('USDT', '')
    
    if elapsed < 60:
        time_str = f"{int(elapsed)} сек."
    else:
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        time_str = f"{minutes} мин. {seconds} сек."
    
    # BOS інформація
    bos_text = ""
    if structure['bos']:
        for b in structure['bos']:
            bos_text += f"\n📊 *{b['label']}:* {format_price(b['level'])} USDT"
    
    # Свінги
    swings_text = ""
    if structure['swings_high']:
        last_hh = structure['swings_high'][-1]
        swings_text += f"\n📈 *HH:* {format_price(last_hh['price'])} USDT"
    if structure['swings_low']:
        last_ll = structure['swings_low'][-1]
        swings_text += f"\n📉 *LL:* {format_price(last_ll['price'])} USDT"
    
    # Фібоначчі
    fib_text = ""
    for level, price in fib_levels.items():
        if level == FIB_LEVEL:
            fib_text += f"\n🎯 *{level:.1%}:* {format_price(price)} USDT ← ВХІД"
        elif level == 0.0 or level == 1.0:
            fib_text += f"\n🏁 *{level:.1%}:* {format_price(price)} USDT ← ТЕЙК"
        else:
            fib_text += f"\n📊 *{level:.1%}:* {format_price(price)} USDT"
    
    message = (
        f"{emoji} *{symbol}* ({coin_name}) {action} на *{change_text}%* за последние {time_str}\n"
        f"\n"
        f"📈 *Імпульс:* {format_price(low)} → {format_price(high)} USDT\n"
        f"📉 *Рух:* {format_price(start_price)} → {format_price(current_price)} USDT\n"
        f"\n"
        f"📊 *BOS таймфрейм:* {bos_tf}\n"
        f"{bos_text}\n"
        f"{swings_text}\n"
        f"\n"
        f"🎯 *Рівень входу (0.618):* {format_price(entry)} USDT\n"
        f"🛑 *Stop Loss:* {format_price(sl)} USDT\n"
        f"🏁 *Take Profit:* {format_price(tp)} USDT\n"
        f"\n"
        f"📊 *Рівні Фібоначчі:*\n"
        f"{fib_text}\n"
        f"\n"
        f"🕐 *Час:* {get_kyiv_time()}"
    )
    
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
        print(f"✅ СИГНАЛ: {symbol} {action} {move:.2f}%")
    except Exception as e:
        print(f"❌ Помилка відправки: {e}")

async def find_moves():
    global tracked_moves
    print(f"🔍 Пошук рухів... {get_kyiv_time()}")
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=15) as resp:
                tickers = await resp.json()
                for item in tickers:
                    symbol = item.get('symbol', '')
                    if not symbol.endswith('USDT'):
                        continue
                    change_24h = float(item.get('priceChangePercent', 0))
                    
                    if abs(change_24h) >= PUMP_THRESHOLD and symbol not in tracked_moves:
                        klines = await get_klines(symbol, TIMEFRAME_MAIN, 20)
                        if not klines:
                            continue
                        
                        high = max(klines['highs'][-2:])
                        low = min(klines['lows'][-2:])
                        last_close = klines['closes'][-1]
                        prev_close = klines['closes'][-2]
                        move = ((last_close - prev_close) / prev_close) * 100
                        
                        if abs(move) >= PUMP_THRESHOLD:
                            tracked_moves[symbol] = {
                                'start_price': prev_close,
                                'current_price': last_close,
                                'move': move,
                                'high': high,
                                'low': low,
                                'time': time.time(),
                                'processed': False,
                                'klines_15m': klines
                            }
                            print(f"🔥 РІЗКИЙ РУХ: {symbol} {move:+.2f}%")
        except Exception as e:
            print(f"❌ Помилка: {e}")

async def analyze_and_send():
    global tracked_moves, alerted
    
    for symbol, data in list(tracked_moves.items()):
        if data['processed']:
            continue
        
        move = data['move']
        high = data['high']
        low = data['low']
        start_price = data['start_price']
        current_price = data['current_price']
        elapsed = time.time() - data['time']
        
        # Шукаємо BOS на 5m або 3m (де швидше)
        bos_found = None
        structure = None
        bos_tf = None
        
        for tf in TIMEFRAME_BOS:
            klines_bos = await get_klines(symbol, tf, LOOKBACK)
            if not klines_bos:
                continue
            
            structure = analyze_structure(klines_bos)
            
            if structure['bos']:
                bos_found = structure
                bos_tf = tf
                print(f"✅ {symbol}: BOS знайдено на {tf}")
                break
        
        if not bos_found:
            print(f"⏳ {symbol}: Немає BOS на 5m/3m, чекаємо...")
            continue
        
        # Розраховуємо Фібоначчі
        diff = high - low
        is_pump = move > 0
        
        fib_levels = {}
        fib_values = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
        for level in fib_values:
            if is_pump:
                fib_levels[level] = high - (diff * level)
            else:
                fib_levels[level] = low + (diff * level)
        
        entry = fib_levels[FIB_LEVEL]
        
        # ========== СТРАТЕГІЯ MAULD ==========
        # Вхід = 0.618
        # Тейк = 0.0 (PUMP) або 1.0 (DUMP) — край імпульсу
        # Стоп = за максимумом/мінімумом (як на зображенні)
        
        if is_pump:  # PUMP
            tp = fib_levels[0.0]  # Тейк на початку імпульсу
            sl = low  # Стоп за мінімумом (як на зображенні)
        else:  # DUMP
            tp = fib_levels[1.0]  # Тейк на початку імпульсу
            sl = high  # Стоп за максимумом (як на зображенні)
        
        # Надсилаємо сигнал
        await send_signal(
            symbol, move, entry, sl, tp,
            start_price, current_price, elapsed,
            fib_levels,
            bos_found,
            high, low,
            bos_tf
        )
        
        data['processed'] = True
        alerted.add(symbol)

async def main():
    print("=" * 50)
    print("🚀 MaulD BOT — СТРАТЕГІЯ ЗА ВАШИМ ДОКУМЕНТОМ")
    print("=" * 50)
    print(f"📊 Поріг руху: {PUMP_THRESHOLD}% на {TIMEFRAME_MAIN}")
    print(f"📈 BOS таймфрейми: {', '.join(TIMEFRAME_BOS)}")
    print(f"🎯 Вхід: {FIB_LEVEL:.1%} (відкат)")
    print(f"🏁 Тейк: край імпульсу (0.0/1.0)")
    print(f"🛑 Стоп: за максимумом/мінімумом")
    print(f"🔄 Перевірка кожні {CHECK_INTERVAL}с")
    print("=" * 50)
    
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"✅ *MaulD BOT запущено!*\n"
                 f"📊 Поріг: {PUMP_THRESHOLD}% на {TIMEFRAME_MAIN}\n"
                 f"📈 BOS: {', '.join(TIMEFRAME_BOS)}\n"
                 f"🎯 Вхід: {FIB_LEVEL:.1%}\n"
                 f"🏁 Тейк: край імпульсу\n"
                 f"🛑 Стоп: за максимумом/мінімумом\n"
                 f"🕐 Київ: {get_kyiv_time()}",
            parse_mode="Markdown"
        )
        print("✅ Бот запущено!")
    except Exception as e:
        print(f"⚠️ Помилка: {e}")
    
    while True:
        try:
            await find_moves()
            await analyze_and_send()
            
            current_time = time.time()
            for symbol, data in list(tracked_moves.items()):
                if current_time - data['time'] > 7200:
                    del tracked_moves[symbol]
            
            await asyncio.sleep(CHECK_INTERVAL)
        except Exception as e:
            print(f"❌ Помилка в циклі: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
