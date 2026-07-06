#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import io
import logging
import asyncio
import os
from datetime import datetime, timedelta

# ===== АВТОУСТАНОВКА (без moexalgo) =====
required_packages = [
    "matplotlib",
    "pandas",
    "python-telegram-bot[job-queue]",
    "apscheduler",
    "httpx"
]

for package in required_packages:
    try:
        if package.startswith("python-telegram-bot"):
            import telegram
        elif package == "pandas":
            import pandas
        elif package == "matplotlib":
            import matplotlib
        elif package == "apscheduler":
            import apscheduler
        elif package == "httpx":
            import httpx
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package.split("[")[0]])

import pandas as pd
import matplotlib.pyplot as plt
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======= БЛОКИРОВКА =======
LOCK_FILE = "bot.lock"
def check_and_create_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                pid = int(f.read().strip())
            try:
                os.kill(pid, 0)
                print(f"❌ Бот уже запущен (PID {pid}). Удалите {LOCK_FILE}.")
                sys.exit(1)
            except OSError:
                print(f"⚠️ Мёртвый PID {pid}, удаляю блокировку.")
                os.remove(LOCK_FILE)
        except:
            os.remove(LOCK_FILE)
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    print(f"✅ Блокировка создана (PID {os.getpid()})")
check_and_create_lock()

# ======= НАСТРОЙКИ =======
TELEGRAM_TOKEN = "8885452009:AAFBmG8idkXGSs_TBA0n-_9GkiR1WmA1-_4"
CHAT_ID = 1234329121
UPDATE_INTERVAL_MINUTES = 60
DAYS_BACK = 14

PAIRS = {
    "Мечел": ("MTLR", "MTLRP"),
    "Татнефть": ("TATN", "TATNP"),
}
DEFAULT_PAIR = "Мечел"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("🏠 Старт"), KeyboardButton("📊 Пары")]],
    resize_keyboard=True,
    one_time_keyboard=False
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ======= ЗАГРУЗКА ДАННЫХ ЧЕРЕЗ MOEX ISS (без moexalgo) =======
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

# ======= ГЕНЕРАЦИЯ ГРАФИКА =======
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

# ======= ОТПРАВКА =======
async def send_spread_update(chat_id, pair_name, bot):
    try:
        logger.info(f"Генерация {pair_name} для {chat_id}")
        loop = asyncio.get_running_loop()
        buf, err = await loop.run_in_executor(None, generate_spread_plot_sync, pair_name)
        if err:
            await bot.send_message(chat_id=chat_id, text=f"❌ {err}")
            return
        await bot.send_photo(chat_id=chat_id, photo=buf, caption=f"📊 Спред {pair_name}")
        logger.info(f"График {pair_name} отправлен")
    except Exception as e:
        logger.exception(f"Ошибка: {e}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Критическая ошибка: {e}")

# ======= АВТООТПРАВКА =======
async def auto_send_all(bot):
    logger.info("Автоотправка всех пар")
    for name in PAIRS:
        await send_spread_update(CHAT_ID, name, bot)
        await asyncio.sleep(3)

# ======= КОМАНДЫ (остались без изменений) =======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start от {user.id}")
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    text = f"📈 Бот спредов.\nАвтоотправка каждые {UPDATE_INTERVAL_MINUTES} мин.\nДоступные пары:\n" + "\n".join(f"• {n} ({t1}/{t2})" for n, (t1, t2) in PAIRS.items())
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await update.message.reply_text("Кнопки управления 👇", reply_markup=MAIN_KEYBOARD)

async def pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    await update.message.reply_text("Выберите пару:", reply_markup=InlineKeyboardMarkup(keyboard))
    await update.message.reply_text("Кнопки 👇", reply_markup=MAIN_KEYBOARD)

async def spread_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔄 Загружаю {DEFAULT_PAIR}...")
    await send_spread_update(update.effective_chat.id, DEFAULT_PAIR, context.bot)
    await update.message.reply_text("Кнопки 👇", reply_markup=MAIN_KEYBOARD)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pair_name = query.data.replace("pair_", "")
    if pair_name not in PAIRS:
        await query.edit_message_text("❌ Неизвестная пара")
        return
    await query.edit_message_text(f"⏳ Генерирую {pair_name}...")
    await send_spread_update(update.effective_chat.id, pair_name, context.bot)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Кнопки 👇", reply_markup=MAIN_KEYBOARD)

async def handle_main_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "🏠 Старт":
        await start(update, context)
    elif text == "📊 Пары":
        await pairs_command(update, context)
    else:
        await update.message.reply_text("Используйте кнопки", reply_markup=MAIN_KEYBOARD)

# ======= ЗАПУСК =======
async def main():
    temp = Application.builder().token(TELEGRAM_TOKEN).build()
    await temp.bot.delete_webhook()
    logger.info("Вебхук удалён.")
    request = HTTPXRequest(read_timeout=30, write_timeout=30, connect_timeout=30, pool_timeout=30)
    app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pairs", pairs_command))
    app.add_handler(CommandHandler("spread", spread_cmd))
    app.add_handler(CallbackQueryHandler(button_callback, pattern="^pair_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_keyboard))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_send_all, 'interval', minutes=UPDATE_INTERVAL_MINUTES, args=[app.bot])
    scheduler.start()
    logger.info(f"Планировщик запущен, интервал {UPDATE_INTERVAL_MINUTES} мин.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(bootstrap_retries=5, allowed_updates=['message', 'callback_query', 'my_chat_member', 'chat_member'])
    logger.info(f"✅ Бот запущен. Пары: {list(PAIRS.keys())}.")
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        scheduler.shutdown()
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)

if __name__ == "__main__":
    asyncio.run(main())