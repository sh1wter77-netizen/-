# main.py
import asyncio
import logging
import random
import string
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
import ccxt.async_support as ccxt

import config

logging.basicConfig(level=logging.INFO)
bot = Bot(token=config.API_TOKEN)
dp = Dispatcher()

DB_FILE = "data/bot_data.db"  # Папка data в Amvera сохраняет файлы навсегда

def init_db():
    import os
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, expires_at TEXT,
            btc_usdt INTEGER DEFAULT 0, eth_usdt INTEGER DEFAULT 0,
            tf_30m INTEGER DEFAULT 0, tf_1h INTEGER DEFAULT 0,
            tf_4h INTEGER DEFAULT 0, tf_1d INTEGER DEFAULT 0,
            fvg INTEGER DEFAULT 0, order_block INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('CREATE TABLE IF NOT EXISTS active_keys (key_code TEXT PRIMARY KEY)')
    cursor.execute('CREATE TABLE IF NOT EXISTS system_config (param_name TEXT PRIMARY KEY, param_value TEXT)')
    cursor.execute("INSERT OR IGNORE INTO system_config (param_name, param_value) VALUES ('free_slots', '3')")
    conn.commit()
    conn.close()

def get_user_data(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    conn.close()
    
    expires_at = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S") if row[1] else None
    return {
        "user_id": row[0], "expires_at": expires_at,
        "coins": {"BTC/USDT": bool(row[2]), "ETH/USDT": bool(row[3])},
        "timeframes": {"30m": bool(row[4]), "1H": bool(row[5]), "4H": bool(row[6]), "1D": bool(row[7])},
        "zones": {"FVG": bool(row[8]), "Order Block": bool(row[9])}
    }

def update_user_setting(user_id, category, item_name, value):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    mapping = {
        ("coins", "BTC/USDT"): "btc_usdt", ("coins", "ETH/USDT"): "eth_usdt",
        ("timeframes", "30m"): "tf_30m", ("timeframes", "1H"): "tf_1h",
        ("timeframes", "4H"): "tf_4h", ("timeframes", "1D"): "tf_1d",
        ("zones", "FVG"): "fvg", ("zones", "Order Block"): "order_block"
    }
    column = mapping.get((category, item_name))
    if column:
        cursor.execute(f"UPDATE users SET {column} = ? WHERE user_id = ?", (1 if value else 0, user_id))
        conn.commit()
    conn.close()

def check_access(user_id) -> bool:
    user = get_user_data(user_id)
    if user["expires_at"] is None: return False
    if datetime.now() < user["expires_at"]: return True
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET expires_at = NULL WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return False

def activate_user_subscription(user_id, days=30):
    expire_date = datetime.now() + timedelta(days=days)
    expire_str = expire_date.strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET expires_at = ? WHERE user_id = ?", (expire_str, user_id))
    conn.commit()
    conn.close()
    return expire_date

def db_add_key(key_code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO active_keys (key_code) VALUES (?)", (key_code,))
    conn.commit()
    conn.close()

def db_use_key(key_code) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM active_keys WHERE key_code = ?", (key_code,))
    if cursor.fetchone():
        cursor.execute("DELETE FROM active_keys WHERE key_code = ?", (key_code,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def try_take_promo_slot() -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT param_value FROM system_config WHERE param_name = 'free_slots'")
    slots = int(cursor.fetchone()[0])
    if slots > 0:
        slots -= 1
        cursor.execute("UPDATE system_config SET param_value = ? WHERE param_name = 'free_slots'", (str(slots),))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def get_all_active_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, expires_at FROM users WHERE expires_at IS NOT NULL")
    rows = cursor.fetchall()
    conn.close()
    active_ids = []
    for row in rows:
        if datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S") > datetime.now():
            active_ids.append(row[0])
    return active_ids

# ================= АНАЛИЗАТОР РЫНКА =================

class MarketAnalyser:
    def find_fvg(self, ohlcv) -> list:
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'high', 'low', 'close', 'v'])
        fvg_signals = []
        if len(df) < 4: return fvg_signals
        i = len(df) - 2 
        if df.loc[i+1, 'low'] > df.loc[i-1, 'high'] and df.loc[i, 'close'] > df.loc[i, 'o']:
            fvg_signals.append({'type': 'Bullish FVG', 'start': df.loc[i-1, 'high'], 'end': df.loc[i+1, 'low']})
        if df.loc[i+1, 'high'] < df.loc[i-1, 'low'] and df.loc[i, 'close'] < df.loc[i, 'o']:
            fvg_signals.append({'type': 'Bearish FVG', 'start': df.loc[i-1, 'low'], 'end': df.loc[i+1, 'high']})
        return fvg_signals

    def find_order_blocks(self, ohlcv) -> list:
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'high', 'low', 'close', 'v'])
        ob_signals = []
        if len(df) < 33: return ob_signals
        i = len(df) - 3 
        hist = df.loc[i - 30 : i - 1]
        if df.loc[i, 'low'] < hist['low'].min() and df.loc[i+1, 'close'] > df.loc[i, 'high']:
            ob_signals.append({'type': 'Bullish Order Block', 'start': df.loc[i, 'low'], 'end': df.loc[i, 'high']})
        if df.loc[i, 'high'] > hist['high'].max() and df.loc[i+1, 'close'] < df.loc[i, 'low']:
            ob_signals.append({'type': 'Bearish Order Block', 'start': df.loc[i, 'low'], 'end': df.loc[i, 'high']})
        return ob_signals

analyser = MarketAnalyser()

# ================= КЛАВИАТУРЫ И ХЭНДЛЕРЫ =================

def get_main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="💎 Получить доступ", callback_data="get_access"))
    builder.row(types.InlineKeyboardButton(text="📋 Информация о боте", callback_data="bot_info"),
                types.InlineKeyboardButton(text="💬 Поддержка", url=f"https://t.me{config.SUPPORT_USERNAME}"))
    return builder.as_markup()

def get_cabinet_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="💲 Выбрать монеты", callback_data="menu_coins"))
    builder.row(types.InlineKeyboardButton(text="📊 Выбрать ТФ", callback_data="menu_tf"))
    builder.row(types.InlineKeyboardButton(text="🔍 Выбрать зоны интереса", callback_data="menu_zones"))
    return builder.as_markup()

def get_settings_keyboard(user_id, category: str):
    user_data = get_user_data(user_id)
    builder = InlineKeyboardBuilder()
    for item_name, is_enabled in user_data[category].items():
        text = f"{item_name} " + ("✅" if is_enabled else "❌")
        builder.row(types.InlineKeyboardButton(text=text, callback_data=f"toggle_{category}_{item_name}"))
    builder.row(types.InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_cabinet"))
    return builder.as_markup()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    welcome_text = "👋 <b>ДОБРО ПОЖАЛОВАТЬ В SMART MONEY ALERT BOT</b>\n\nБот отслеживает зоны FVG и Order Blocks.\n\n"
    if user_data["expires_at"] is None and try_take_promo_slot():
        activate_user_subscription(user_id, 30)
        welcome_text += "🎁 Вы успели на акцию! Вам предоставлен 1 месяц бесплатного доступа!\n\n"
        await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_cabinet_keyboard())
        return
    if check_access(user_id):
        date_str = get_user_data(user_id)["expires_at"].strftime("%d.%m.%Y")
        welcome_text += f"🎯 Ваша подписка активна до: <b>{date_str}</b>"
        await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_cabinet_keyboard())
    else:
        welcome_text += "📍 Нажмите <b>\"Получить доступ\"</b> и отправьте полученный ключ."
        await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_main_menu_keyboard())

@dp.message(Command("generate"))
async def cmd_generate_key(message: types.Message):
    if message.from_user.id != config.ADMIN_ID: return
    key = "SMART-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    db_add_key(key)
    await message.answer(f"🔑 Создан ключ: <code>{key}</code>", parse_mode="HTML")

@dp.callback_query(F.data == "get_access")
async def show_access(callback: types.CallbackQuery):
    if check_access(callback.from_user.id):
        await callback.message.edit_text("🎯 Доступ активен. Настройте фильтры:", reply_markup=get_cabinet_keyboard())
    else:
        text = f"💎 <b>ОФОРМЛЕНИЕ ДОСТУПА</b>\n\n{config.PAYMENT_REQUISITES}\n\n👉 После оплаты отправьте ключ текстовым сообщением."
        builder = InlineKeyboardBuilder().row(types.InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_start"))
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
@dp.callback_query(F.data == "bot_info")async def show_info(callback: types.CallbackQuery):text = "📋 ИНФОРМАЦИЯ О БОТЕ\n\nБот ищет импульсные FVG и разворотные Order Block на экстремумах."await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_main_menu_keyboard())@dp.callback_query(F.data.in_(["back_to_start", "back_to_cabinet", "menu_coins", "menu_tf", "menu_zones"]))async def menu_navigation(callback: types.CallbackQuery):if callback.data == "back_to_start":await callback.message.edit_text("📍 Выберите действие:", reply_markup=get_main_menu_keyboard())elif callback.data == "back_to_cabinet":await callback.message.edit_text("🎯 Меню настроек фильтрации:", reply_markup=get_cabinet_keyboard())else:cat = "coins" if callback.data == "menu_coins" else "timeframes" if callback.data == "menu_tf" else "zones"await callback.message.edit_text("⚙️ Выберите параметры:", reply_markup=get_settings_keyboard(callback.from_user.id, cat))@dp.callback_query(F.data.startswith("toggle_"))async def toggle_setting(callback: types.CallbackQuery):, category, item = callback.data.split("", 2)user_data = get_user_data(callback.from_user.id)current_val = user_data[category][item]update_user_setting(callback.from_user.id, category, item, not current_val)await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(callback.from_user.id, category))@dp.message(F.text)async def handle_keys(message: types.Message):user_id = message.from_user.idif check_access(user_id): returnif db_use_key(message.text.strip()):activate_user_subscription(user_id, 30)await message.answer("🎉 Доступ успешно активирован на 30 дней!", reply_markup=get_cabinet_keyboard())else:await message.answer("❌ Неверный код подписки. Обратитесь в поддержку.")================= СКАНЕР БИРЖИ =================async def market_scanner():exchange = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})last_sent_signals = {}while True:try:active_users = get_all_active_users()if not active_users:await asyncio.sleep(60)continuefor symbol in ['BTC/USDT', 'ETH/USDT']:for tf in ['30m', '1h', '4h', '1d']:candles = await exchange.fetch_ohlcv(symbol, tf.lower(), limit=50)fvg_list = analyser.find_fvg(candles)ob_list = analyser.find_order_blocks(candles)all_found = []for f in fvg_list: all_found.append(("FVG", f['type'], f['start'], f['end']))for o in ob_list: all_found.append(("Order Block", o['type'], o['start'], o['end']))for zone_cat, zone_name, start, end in all_found:sig_key = f"{symbol}{tf}{zone_name}{start}{end}"if sig_key in last_sent_signals: continuelast_sent_signals[sig_key] = Trueemoji = "🟢" if "Bullish" in zone_name else "🔴"tv_url = f"tradingview.com:{symbol.replace('/', '')}"msg_text = (f"{emoji} ОБНАРУЖЕНА ЗОНА ИНТЕРЕСА\n\n📊 Монета: {symbol}\n"f"⏱ Таймфрейм: {tf}\n🔍 Паттерн: {zone_name}\n"f"💵 Диапазон: {start:.2f} - {end:.2f}\n\n"f"⚠️ Цена сформировала сильную область! Наблюдайте за реакцией.")builder = InlineKeyboardBuilder().row(types.InlineKeyboardButton(text="📈 Открыть TradingView", url=tv_url))for uid in active_users:ud = get_user_data(uid)if ud["coins"].get(symbol) and ud["timeframes"].get(tf) and ud["zones"].get(zone_cat):try: await bot.send_message(uid, msg_text, parse_mode="HTML", reply_markup=builder.as_markup())except Exception: passawait asyncio.sleep(60)except Exception as e:logging.error(f"Ошибка сканера: {e}")await asyncio.sleep(30)async def main():init_db()asyncio.create_task(market_scanner())await dp.start_polling(bot)if name == 'main':asyncio.run(main())
