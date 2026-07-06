#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import io
import logging
import os
import sys
from datetime import datetime, timedelta

# ===== УСТАНАВЛИВАЕМ НЕДОСТАЮЩИЕ ПАКЕТЫ (без aiogram – он уже есть) =====
required_packages = [
    "matplotlib",
    "pandas",
    "httpx",
    "apscheduler"
]

for package in required_packages:
    try:
        if package == "matplotlib":
            import matplotlib
        elif package == "pandas":
            import pandas
        elif package == "httpx":
            import httpx
        elif package == "apscheduler":
            import apscheduler
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])

import pandas as pd
import matplotlib.pyplot as plt
import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======= БЛОКИРОВКА (защита от двойного запуска) =======
LOCK_FILE = "bot.lock"
try:
    import fcntl
    lock_fd = open(LOCK_FILE, "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except (ImportError, OSError):
    try:
        if os.name == "nt":
            import msvcrt
            lock_fd = open(LOCK_FILE, "w")
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            raise
    except Exception:
        print("❌ Бот уже запущен! Удалите bot.lock и завершите старый процесс.")
        sys.exit(1)

# ======= НАСТРОЙКИ =======
TOKEN = "8885452009:AAFBmG8idkXGSs_TBA0n-_9GkiR1WmA1-_4"
CHAT_ID = 1234329121
UPDATE_INTERVAL_MINUTES = 60
DAYS_BACK = 14

PAIRS = {
    "Мечел": ("MTLR", "MTLRP"),
    "Татнефть": ("TATN", "TATNP"),
}
DEFAULT_PAIR = "Мечел"

# ======= КЛАВИАТУРА =======
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🏠 Старт"), KeyboardButton("📊 Пары")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)

# ======= ЛОГИРОВАНИЕ =======
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ======= БОТ И ДИСПЕТЧЕР =======
bot = Bot(token=TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# ======= ПЛАНИРОВЩИК =======
scheduler = AsyncIOScheduler()

# ======= ФУНКЦИИ ЗАГРУЗКИ ДАННЫХ (как раньше, через httpx) =======
async def fetch_candles(ticker, start_date, end_date, interval='15'):
    url = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/tqbr/securities/{ticker}/candles.json"
    params = {'from': start_date.strftime('%Y-%m-%d'), 'till': end_date.strftime('%Y-%m-%d'), 'interval': interval, 'iss.meta': 'off'}
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    columns = data['candles']['columns']
    rows = data['candles']['data']
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=columns)
    df['begin'] = pd.to_datetime(df['begin'])
    df.set_index('begin', inplace=True)
    return df

async def get_spread_data(ticker1, ticker2, start_date, end_date):
    try:
        logger.info(f"Запрос {ticker1}/{ticker2} с {start_date} по {end_date}")
        df1 = await fetch_candles(ticker1, start_date, end_date, '15')
        df2 = await fetch_candles(ticker2, start_date, end_date, '15')
        intraday = True
        if df1.empty or df2.empty:
            logger.warning("15min пусто, берём дневные")
            df1 = await fetch_candles(ticker1, start_date, end_date, '24')
            df2 = await fetch_candles(ticker2, start_date, end_date, '24')
            intraday = False
            if df1.empty or df2.empty:
                return None
        combined = pd.DataFrame({f'{ticker1}_close': df1['close'], f'{ticker2}_close': df2['close']}).dropna()
        if intraday:
            combined = combined[combined.index.hour >= 7]
        if combined.empty:
            return None
        combined['Spread'] = combined[f'{ticker1}_close'] - combined[f'{ticker2}_close']
        logger.info(f"Спред: {len(combined)} точек")
        return combined
    except Exception as e:
        logger.exception(f"Ошибка: {e}")
        return None

# ======= ГЕНЕРАЦИЯ ГРАФИКА (синхронная, запускается в executor) =======
def generate_spread_plot_sync(pair_name):
    if pair_name not in PAIRS:
        return None, f"Пара '{pair_name}' не найдена."
    ticker1, ticker2 = PAIRS[pair_name]
    now = datetime.now()
    end_date = now.date() + timedelta(days=1)
    start_date = end_date - timedelta(days=DAYS_BACK)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        data = loop.run_until_complete(get_spread_data(ticker1, ticker2, start_date, end_date))
    finally:
        loop.close()
    if data is None or data.empty:
        return None, f"Нет данных за последние {DAYS_BACK} дней по {pair_name}."
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(range(len(data)), data['Spread'].values, linewidth=1.8, label=f'Спред {ticker1}–{ticker2}')
    step = max(1, len(data)//15)
    ax.set_xticks(list(range(0, len(data), step)))
    ax.set_xticklabels([data.index[i].strftime("%m-%d %H:%M") for i in range(0, len(data), step)], rotation=45, ha='right', fontsize=8)
    ax.set_title(f'{pair_name} — спред ({ticker1}–{ticker2})', fontsize=14)
    ax.set_ylabel('Разница (₽)')
    ax.grid(True, alpha=0.3)
    ax.legend()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf, None

# ======= ОТПРАВКА ГРАФИКА =======
async def send_spread_update(chat_id, pair_name):
    try:
        logger.info(f"Генерация {pair_name} для {chat_id}")
        loop = asyncio.get_running_loop()
        buf, err = await loop.run_in_executor(None, generate_spread_plot_sync, pair_name)
        if err:
            await bot.send_message(chat_id, f"❌ {err}")
            return
        await bot.send_photo(chat_id, photo=buf, caption=f"📊 Спред {pair_name}")
        logger.info(f"График {pair_name} отправлен")
    except Exception as e:
        logger.exception(f"Ошибка: {e}")
        await bot.send_message(chat_id, f"❌ Критическая ошибка: {e}")

# ======= АВТООТПРАВКА =======
async def auto_send_all():
    logger.info("Автоотправка всех пар")
    for name in PAIRS:
        await send_spread_update(CHAT_ID, name)
        await asyncio.sleep(3)

# ======= ОБРАБОТЧИКИ КОМАНД =======
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    user = message.from_user
    logger.info(f"/start от {user.id} (@{user.username})")
    keyboard = InlineKeyboardMarkup(row_width=1)
    for name in PAIRS:
        keyboard.add(InlineKeyboardButton(name, callback_data=f"pair_{name}"))
    text = (f"📈 Бот спредов.\nАвтоотправка каждые {UPDATE_INTERVAL_MINUTES} мин.\n\nДоступные пары:\n" +
            "\n".join(f"• {n} ({t1}/{t2})" for n, (t1, t2) in PAIRS.items()))
    await message.reply(text, reply_markup=keyboard)
    await message.reply("Кнопки управления 👇", reply_markup=MAIN_KEYBOARD)

@dp.message_handler(commands=['pairs'])
async def cmd_pairs(message: types.Message):
    keyboard = InlineKeyboardMarkup(row_width=1)
    for name in PAIRS:
        keyboard.add(InlineKeyboardButton(name, callback_data=f"pair_{name}"))
    await message.reply("Выберите пару:", reply_markup=keyboard)
    await message.reply("Кнопки 👇", reply_markup=MAIN_KEYBOARD)

@dp.message_handler(commands=['spread'])
async def cmd_spread(message: types.Message):
    await message.reply(f"🔄 Загружаю {DEFAULT_PAIR}...")
    await send_spread_update(message.chat.id, DEFAULT_PAIR)
    await message.reply("Кнопки 👇", reply_markup=MAIN_KEYBOARD)

@dp.message_handler(lambda msg: msg.text in ["🏠 Старт", "📊 Пары"])
async def handle_main_keyboard(message: types.Message):
    if message.text == "🏠 Старт":
        await cmd_start(message)
    elif message.text == "📊 Пары":
        await cmd_pairs(message)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('pair_'))
async def process_callback(callback_query: types.CallbackQuery):
    await callback_query.answer()
    pair_name = callback_query.data.replace("pair_", "")
    if pair_name not in PAIRS:
        await callback_query.message.edit_text("❌ Неизвестная пара")
        return
    await callback_query.message.edit_text(f"⏳ Генерирую {pair_name}...")
    await send_spread_update(callback_query.message.chat.id, pair_name)
    await bot.send_message(callback_query.message.chat.id, "Кнопки 👇", reply_markup=MAIN_KEYBOARD)

# ======= ЗАПУСК =======
async def on_startup(dp):
    # Удаляем вебхук (на всякий случай)
    await bot.delete_webhook()
    logger.info("Вебхук удалён.")
    # Запускаем планировщик
    scheduler.add_job(auto_send_all, 'interval', minutes=UPDATE_INTERVAL_MINUTES)
    scheduler.start()
    logger.info(f"Планировщик запущен, интервал {UPDATE_INTERVAL_MINUTES} мин.")

async def on_shutdown(dp):
    scheduler.shutdown()
    # Снимаем блокировку
    if os.name == "nt":
        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
    logger.info("Бот остановлен.")

if __name__ == '__main__':
    from aiogram import executor
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown)