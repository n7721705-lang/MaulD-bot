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
TIMEFRAME_BOS = "5m"          # Таймфрейм для BOS
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
    """Отримує свічки з Binance Futures"""
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
    """Знаходить свінг-хай та свінг-лоу"""
    swings_high = []
    swings_low = []
    
    for i in range(lookback, len(highs) - lookback):
        if all(highs[i] > highs[i-j] for j in range(1, lookback+1)) and \
           all(highs[i] > highs[i+j] for j in range(1, lookback+1)):
            swings_high.append((i, highs[i]))
        
        if all(lows[i] < lows[i-j] for j in range(1, lookback+1)) and \
           all(lows[i] < lows[i+j] for j in range(1, lookback+1)):
            swings_low.append((i, lows[i]))
    
    return swings_high, swings_low

def detect_bos(highs, lows, swings_high, swings_low):
    """Визначає BOS (злам структури)"""
    bos_signals = []
    
    if len(swings_high) >= 2:
        last_hh = swings_high[-2]
        if highs[-1] > last_hh[1]:
            bos_signals.append({'type': 'BOS_UP', 'level': last_hh[1]})
    
    if len(swings_low) >= 2:
        last_ll = swings_low[-2]
        if lows[-1] < last_ll[1]:
            bos_signals.append({'type': 'BOS_DOWN', 'level': last_ll[1]})
    
    return bos_signals

def analyze_structure(highs, lows, closes):
    """Аналізує структуру ринку"""
    swings_high, swings_low = find_swings(highs, lows)
    bos = detect_bos(highs, lows, swings_high, swings_low)
    
    return {
        'swings_high': swings_high,
        'swings_low': swings_low,
        'bos': bos
    }

async def send_signal(symbol, move, entry, sl, tp, start_price, current_price, elapsed, fib_levels, bos_signals):
    """Надсилає сигнал у Telegram"""
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
    if bos_signals:
        bos_types = []
        for b in bos_signals:
            if b['type'] == 'BOS_UP':
                bos_types.append('📈 BOS ВГОРУ')
            else:
                bos_types.append('📉 BOS ВНИЗ')
        bos_text = "\n📊 *BOS:* " + ", ".join(bos_types)
    
    # Рівні Фібоначчі
    fib_text = ""
    for level, price in fib_levels.items():
        if level == FIB_LEVEL:
            fib_text += f"\n🎯 *{level:.1%}:* {format_price(price)} USDT ← ВХІД"
        elif level in [0.0, 1.0]:
            fib_text += f"\n📊 *{level:.1%}:* {format_price(price)} USDT"
    
    message = (
        f"{emoji} *{symbol}* ({coin_name}) {action} на *{change_text}%* за последние {time_str}\n"
        f"\n"
        f"📊 *Рівень входу (0.618):* {format_price(entry)} USDT\n"
        f"🛑 *Stop Loss:* {format_price(sl)} USDT\n"
        f"🎯 *Take Profit:* {format_price(tp)} USDT\n"
        f"{bos_text}\n"
        f"\n"
        f"📈 *Рух:* {format_price(start_price)} → {format_price(current_price)} USDT\n"
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
    """Шукає різкі рухи на 15m таймфреймі"""
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
    """Аналізує рух та надсилає сигнал"""
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
        
        # Отримуємо свічки для аналізу BOS (5m)
        klines_bos = await get_klines(symbol, TIMEFRAME_BOS, LOOKBACK)
        if not klines_bos:
            continue
        
        # Аналізуємо структуру
        structure = analyze_structure(
            klines_bos['highs'],
            klines_bos['lows'],
            klines_bos['closes']
        )
        
        # Перевіряємо BOS
        if not structure['bos']:
            print(f"⏳ {symbol}: Немає BOS, чекаємо...")
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
        
        # Розраховуємо SL та TP
        if is_pump:
            sl = low - (diff * 0.1)
            tp = entry + (entry - sl) * 2
        else:
            sl = high + (diff * 0.1)
            tp = entry - (sl - entry) * 2
        
        # Надсилаємо сигнал
        await send_signal(
            symbol, move, entry, sl, tp,
            start_price, current_price, elapsed,
            fib_levels,
            structure['bos']
        )
        
        data['processed'] = True
        alerted.add(symbol)

async def main():
    print("=" * 50)
    print("🚀 MaulD BOT — PUMP/DUMP STRATEGY")
    print("=" * 50)
    print(f"📊 Поріг руху: {PUMP_THRESHOLD}%")
    print(f"⏱ Таймфрейм: {TIMEFRAME_MAIN}")
    print(f"📈 BOS таймфрейм: {TIMEFRAME_BOS}")
    print(f"🎯 Рівень Фібоначчі: {FIB_LEVEL:.1%}")
    print(f"🔄 Перевірка кожні {CHECK_INTERVAL}с")
    print("=" * 50)
    
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"✅ *MaulD BOT запущено!*\n"
                 f"📊 Поріг: {PUMP_THRESHOLD}%\n"
                 f"🎯 Рівень входу: {FIB_LEVEL:.1%}\n"
                 f"📈 BOS таймфрейм: {TIMEFRAME_BOS}\n"
                 f"🕐 Київ: {get_kyiv_time()}",
            parse_mode="Markdown"
        )
        print("✅ Бот запущено!")
    except Exception as e:
        print(f"⚠️ Помилка відправки тестового повідомлення: {e}")
    
    while True:
        try:
            await find_moves()
            await analyze_and_send()
            
            # Очищуємо старі рухи (> 2 годин)
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
