import asyncio
import time
import aiohttp
from datetime import datetime, timezone, timedelta
from telegram import Bot

# ==================== НАЛАШТУВАННЯ ====================
TELEGRAM_BOT_TOKEN = "8818473462:AAG02pUpdJn0FsBzJabEVdOW7-UrFmMbx4w"
TELEGRAM_CHAT_ID = -1003933274705

# Депозит та ризик
VIRTUAL_BALANCE = 100.0          # Уявний депозит (USDT)
RISK_PER_TRADE = 0.01            # 1% від депозиту на угоду

# Risk/Reward
TP_RATIO = 3.0                   # 1:3

# Таймфрейми
TIMEFRAME_REFERENCE = "1h"       # Опорний таймфрейм
TIMEFRAME_EXECUTION = "5m"       # Таймфрейм для входу
LOOKBACK_CANDLES = 30

# CRT параметри
SL_BUFFER = 0.02

CHECK_INTERVAL = 15              # Аналіз кожні 15 секунд
SCAN_LIMIT = 20                  # Максимум монет за одне сканування
# =====================================================

KYIV_TZ = timezone(timedelta(hours=3))
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# Стан бота
balance = VIRTUAL_BALANCE
open_positions = {}
closed_trades = []
alerted = set()
total_trades = 0
winning_trades = 0
symbol_index = 0
all_symbols = []
last_scan_time = 0

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
            async with session.get(url, timeout=10) as resp:
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

async def get_current_price(symbol):
    url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=5) as resp:
                data = await resp.json()
                return float(data['price'])
        except:
            return None

def find_reference_candle(klines):
    if len(klines) < 2:
        return None
    
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
    if not ref_candle or len(klines) < 3:
        return None
    
    last = {
        'high': klines['highs'][-1],
        'low': klines['lows'][-1],
        'open': klines['opens'][-1],
        'close': klines['closes'][-1]
    }
    
    is_bullish_crt = False
    is_bearish_crt = False
    
    if last['low'] < ref_candle['low'] and last['close'] > ref_candle['low']:
        if last['close'] > last['open']:
            is_bullish_crt = True
    
    if last['high'] > ref_candle['high'] and last['close'] < ref_candle['high']:
        if last['close'] < last['open']:
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

def calculate_position_size(balance, entry, stop_loss, risk_percent=0.01):
    risk_amount = balance * risk_percent
    risk_per_coin = abs(entry - stop_loss)
    if risk_per_coin == 0:
        return 0
    position_size = risk_amount / risk_per_coin
    return position_size

async def open_virtual_position(symbol, setup, atr, current_balance):
    ref = setup['ref_candle']
    entry = setup['entry']
    
    if setup['type'] == 'BULLISH':
        stop_loss = entry * (1 - SL_BUFFER - atr/entry * 0.5)
        take_profit = entry + (entry - stop_loss) * TP_RATIO
        position_type = 'LONG'
    else:
        stop_loss = entry * (1 + SL_BUFFER + atr/entry * 0.5)
        take_profit = entry - (stop_loss - entry) * TP_RATIO
        position_type = 'SHORT'
    
    size = calculate_position_size(current_balance, entry, stop_loss)
    
    if size <= 0:
        return None
    
    position = {
        'symbol': symbol,
        'type': position_type,
        'entry': entry,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'size': size,
        'open_time': time.time(),
        'open_price': entry,
        'risk_amount': current_balance * RISK_PER_TRADE,
        'is_open': True
    }
    
    return position

async def close_virtual_position(position, current_price):
    global balance, total_trades, winning_trades
    
    if position['type'] == 'LONG':
        pnl = (current_price - position['entry']) * position['size']
    else:
        pnl = (position['entry'] - current_price) * position['size']
    
    pnl_percent = (pnl / (position['entry'] * position['size'])) * 100 if position['size'] > 0 else 0
    
    balance += pnl
    
    total_trades += 1
    if pnl > 0:
        winning_trades += 1
    
    trade_result = {
        'symbol': position['symbol'],
        'type': position['type'],
        'entry': position['entry'],
        'exit': current_price,
        'pnl': pnl,
        'pnl_percent': pnl_percent,
        'open_time': position['open_time'],
        'close_time': time.time(),
        'is_win': pnl > 0
    }
    
    return trade_result

async def monitor_positions():
    global open_positions, balance, closed_trades
    
    for symbol, position in list(open_positions.items()):
        if not position['is_open']:
            continue
        
        current_price = await get_current_price(symbol)
        if not current_price:
            continue
        
        if position['type'] == 'LONG':
            if current_price >= position['take_profit']:
                result = await close_virtual_position(position, current_price)
                position['is_open'] = False
                closed_trades.append(result)
                await send_trade_result(result)
                del open_positions[symbol]
                continue
            
            if current_price <= position['stop_loss']:
                result = await close_virtual_position(position, current_price)
                position['is_open'] = False
                closed_trades.append(result)
                await send_trade_result(result)
                del open_positions[symbol]
                continue
        
        else:
            if current_price <= position['take_profit']:
                result = await close_virtual_position(position, current_price)
                position['is_open'] = False
                closed_trades.append(result)
                await send_trade_result(result)
                del open_positions[symbol]
                continue
            
            if current_price >= position['stop_loss']:
                result = await close_virtual_position(position, current_price)
                position['is_open'] = False
                closed_trades.append(result)
                await send_trade_result(result)
                del open_positions[symbol]
                continue

async def send_trade_result(result):
    emoji = "🟢" if result['is_win'] else "🔴"
    status = "ПРИБУТОК ✅" if result['is_win'] else "ЗБИТОК ❌"
    
    coin_name = result['symbol'].replace('USDT', '')
    pnl_text = f"+{result['pnl']:.2f}$" if result['pnl'] > 0 else f"{result['pnl']:.2f}$"
    
    duration = result['close_time'] - result['open_time']
    if duration < 60:
        duration_text = f"{int(duration)} сек."
    elif duration < 3600:
        duration_text = f"{int(duration/60)} хв."
    else:
        duration_text = f"{int(duration/3600)} год."
    
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    
    message = (
        f"{emoji} *{result['symbol']}* ({coin_name}) — УГОДА ЗАКРИТА {status}\n"
        f"\n"
        f"📊 *Тип:* {result['type']}\n"
        f"💰 *P&L:* {pnl_text}\n"
        f"📈 *Зміна:* {result['pnl_percent']:.2f}%\n"
        f"📊 *Вхід:* {format_price(result['entry'])} USDT\n"
        f"📊 *Вихід:* {format_price(result['exit'])} USDT\n"
        f"⏱ *Тривалість:* {duration_text}\n"
        f"\n"
        f"📊 *Статистика:*\n"
        f"   💰 Баланс: {balance:.2f} USDT\n"
        f"   📈 Угоди: {total_trades} | ✅ Win rate: {win_rate:.1f}%\n"
        f"\n"
        f"🕐 *Час:* {get_kyiv_time()}"
    )
    
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
        print(f"✅ РЕЗУЛЬТАТ: {result['symbol']} {pnl_text}")
    except Exception as e:
        print(f"❌ Помилка відправки: {e}")

async def get_all_symbols():
    global all_symbols
    
    if all_symbols:
        return all_symbols
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=15) as resp:
                data = await resp.json()
                symbols = []
                for item in data.get('symbols', []):
                    symbol = item.get('symbol', '')
                    if symbol.endswith('USDT') and item.get('status') == 'TRADING':
                        symbols.append(symbol)
                all_symbols = symbols
                print(f"📊 Завантажено {len(symbols)} ф'ючерсних монет")
                return symbols
        except Exception as e:
            print(f"❌ Помилка завантаження монет: {e}")
            return []

async def scan_crt_setups():
    global balance, open_positions, alerted, symbol_index, last_scan_time
    
    current_time = time.time()
    
    # Перевіряємо, чи минуло 15 секунд
    if current_time - last_scan_time < CHECK_INTERVAL:
        return
    
    last_scan_time = current_time
    
    symbols = await get_all_symbols()
    if not symbols:
        return
    
    # Беремо наступні SCAN_LIMIT монет (циклічно)
    start_idx = symbol_index
    end_idx = min(symbol_index + SCAN_LIMIT, len(symbols))
    batch = symbols[start_idx:end_idx]
    
    # Оновлюємо індекс для наступного сканування
    symbol_index = end_idx % len(symbols)
    
    print(f"🔍 Сканування {len(batch)} монет... {get_kyiv_time()} (індекс: {symbol_index})")
    
    for symbol in batch:
        if symbol in open_positions:
            continue
        
        if symbol in alerted:
            continue
        
        # Отримуємо свічки
        klines_htf = await get_klines(symbol, TIMEFRAME_REFERENCE, LOOKBACK_CANDLES)
        if not klines_htf:
            continue
        
        klines_ltf = await get_klines(symbol, TIMEFRAME_EXECUTION, 30)
        if not klines_ltf:
            continue
        
        ref_candle = find_reference_candle(klines_htf)
        if not ref_candle:
            continue
        
        setup = detect_manipulation(klines_ltf, ref_candle)
        if not setup:
            continue
        
        atr = calculate_atr(klines_ltf)
        if atr == 0:
            continue
        
        position = await open_virtual_position(symbol, setup, atr, balance)
        if not position:
            continue
        
        open_positions[symbol] = position
        alerted.add(symbol)
        
        await send_open_position_signal(symbol, position, setup)
        await asyncio.sleep(0.3)

async def send_open_position_signal(symbol, position, setup):
    emoji = "🟢" if position['type'] == 'LONG' else "🔴"
    direction = "LONG (BUY)" if position['type'] == 'LONG' else "SHORT (SELL)"
    direction_emoji = "📈" if position['type'] == 'LONG' else "📉"
    
    coin_name = symbol.replace('USDT', '')
    risk_amount = balance * RISK_PER_TRADE
    
    rr = TP_RATIO
    pnl_target = risk_amount * rr
    
    message = (
        f"{emoji} *{symbol}* ({coin_name}) — CRT СИГНАЛ {direction_emoji}\n"
        f"\n"
        f"📊 *Напрямок:* {direction} {direction_emoji}\n"
        f"🎯 *Вхід:* {format_price(position['entry'])} USDT\n"
        f"🛑 *Stop Loss:* {format_price(position['stop_loss'])} USDT\n"
        f"🏁 *Take Profit:* {format_price(position['take_profit'])} USDT\n"
        f"📊 *Risk/Reward:* 1:{rr:.0f}\n"
        f"📊 *Розмір позиції:* {position['size']:.4f} USDT\n"
        f"💰 *Ризик:* {risk_amount:.2f} USDT (1% від депозиту)\n"
        f"🏆 *Потенційний прибуток:* {pnl_target:.2f} USDT\n"
        f"📊 *Баланс:* {balance:.2f} USDT\n"
        f"\n"
        f"🕐 *Час:* {get_kyiv_time()}"
    )
    
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
        print(f"✅ ВІДКРИТО: {symbol} {position['type']} RR 1:{rr:.0f}")
    except Exception as e:
        print(f"❌ Помилка відправки: {e}")

async def send_daily_summary():
    global total_trades, winning_trades, balance
    
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    
    message = (
        f"📊 *ЩОДЕННИЙ ЗВІТ*\n"
        f"\n"
        f"💰 *Баланс:* {balance:.2f} USDT\n"
        f"📈 *Зміна:* {((balance - VIRTUAL_BALANCE) / VIRTUAL_BALANCE * 100):.2f}%\n"
        f"📊 *Угоди:* {total_trades}\n"
        f"✅ *Прибуткових:* {winning_trades}\n"
        f"📊 *Win Rate:* {win_rate:.1f}%\n"
        f"🎯 *Risk/Reward:* 1:{TP_RATIO:.0f}\n"
        f"\n"
        f"🕐 *Час:* {get_kyiv_time()}"
    )
    
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
        print("✅ Денний звіт надіслано")
    except Exception as e:
        print(f"❌ Помилка відправки: {e}")

async def main():
    global balance, total_trades, winning_trades, open_positions, all_symbols
    
    print("=" * 50)
    print("🚀 CRT BOT — УЯВНА ТОРГІВЛЯ (RR 1:3)")
    print("=" * 50)
    print(f"💰 Депозит: {VIRTUAL_BALANCE} USDT")
    print(f"📊 Ризик на угоду: {RISK_PER_TRADE*100:.0f}%")
    print(f"🎯 Risk/Reward: 1:{TP_RATIO:.0f}")
    print(f"📊 Опорний таймфрейм: {TIMEFRAME_REFERENCE}")
    print(f"📈 Таймфрейм виконання: {TIMEFRAME_EXECUTION}")
    print(f"🔄 Аналіз кожні {CHECK_INTERVAL} секунд")
    print(f"📊 Монет за сканування: {SCAN_LIMIT}")
    print("=" * 50)
    
    # Завантажуємо всі монети при старті
    print("📡 Завантаження списку монет...")
    all_symbols = await get_all_symbols()
    
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"✅ *CRT BOT запущено!*\n"
                 f"💰 Депозит: {VIRTUAL_BALANCE} USDT\n"
                 f"📊 Ризик: {RISK_PER_TRADE*100:.0f}% на угоду\n"
                 f"🎯 Risk/Reward: 1:{TP_RATIO:.0f}\n"
                 f"📊 Опорний таймфрейм: {TIMEFRAME_REFERENCE}\n"
                 f"📈 Таймфрейм виконання: {TIMEFRAME_EXECUTION}\n"
                 f"🔄 Аналіз кожні {CHECK_INTERVAL} секунд\n"
                 f"📊 Моніторинг {len(all_symbols)} ф'ючерсних монет\n"
                 f"🕐 Київ: {get_kyiv_time()}",
            parse_mode="Markdown"
        )
        print("✅ Бот запущено!")
    except Exception as e:
        print(f"⚠️ Помилка: {e}")
    
    last_daily_report = datetime.now().date()
    
    while True:
        try:
            # Скануємо нові сигнали (кожні 15 секунд)
            await scan_crt_setups()
            
            # Моніторимо відкриті позиції
            await monitor_positions()
            
            # Щоденний звіт
            today = datetime.now().date()
            if today != last_daily_report:
                await send_daily_summary()
                last_daily_report = today
            
            # Якщо сканування не відбулося, чекаємо
            await asyncio.sleep(1)
            
        except Exception as e:
            print(f"❌ Помилка в циклі: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
