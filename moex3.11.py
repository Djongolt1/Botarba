#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import io
import logging
import asyncio
import os
import requests
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

# ===== АВТОУСТАНОВКА БИБЛИОТЕК (без moexalgo) =====
required_packages = [
    "matplotlib",
    "pandas",
    "requests",
    "python-telegram-bot[job-queue]",
    "apscheduler"
]

for package in required_packages:
    try:
        if package.startswith("python-telegram-bot"):
            import telegram
        elif package == "pandas":
            import pandas
        elif package == "matplotlib":
            import matplotlib
        elif package == "requests":
            import requests
        elif package == "apscheduler":
            import apscheduler
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package.split("[")[0]])

# Устанавливаем бэкенд matplotlib для работы без GUI
import matplotlib
matplotlib.use('Agg')

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======= ПРОВЕРКА БЛОКИРОВКИ =======
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
TELEGRAM_TOKEN = "8885452009:AAGxNl0iUCT2Q58jXm0DZ_h-ZDevQcYtqpw"  # замените при необходимости
CHAT_ID = 1234329121
UPDATE_INTERVAL_MINUTES = 60
DAYS_BACK = 14  # максимум 14 дней

PAIRS = {
    "Мечел": ("MTLR", "MTLRP"),
    "Татнефть": ("TATN", "TATNP"),
}
DEFAULT_PAIR = "Мечел"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🏠 Старт"), KeyboardButton("📊 Пары")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ======= ФУНКЦИЯ ЗАГРУЗКИ ДАННЫХ С MOEX ЧЕРЕЗ ISS API =======
def fetch_candles(ticker, start_date, end_date, interval='15'):
    """
    Загружает свечи с MOEX ISS API.
    interval: '15' для 15 минут, '1' для дневных (по умолчанию 15)
    """
    url = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/tqbr/securities/{ticker}/candles.json"
    params = {
        'from': start_date.strftime('%Y-%m-%d'),
        'till': end_date.strftime('%Y-%m-%d'),
        'interval': interval,  # 15 = 15 минут
        'limit': 10000
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        candles = data.get('candles', {})
        if not candles or 'data' not in candles:
            logger.warning(f"Нет данных в ответе для {ticker}")
            return None
        columns = candles['columns']
        rows = candles['data']
        if not rows:
            logger.warning(f"Пустые строки для {ticker}")
            return None
        df = pd.DataFrame(rows, columns=columns)
        df['begin'] = pd.to_datetime(df['begin'])
        df.set_index('begin', inplace=True)
        logger.info(f"Загружено {len(df)} свечей для {ticker}")
        return df
    except Exception as e:
        logger.error(f"Ошибка запроса для {ticker}: {e}")
        return None

def get_spread_data(ticker1, ticker2, start_date, end_date):
    try:
        logger.info(f"Запрос свечей для {ticker1} и {ticker2} с {start_date} по {end_date}")
        # Сначала пробуем 15-минутки
        df1 = fetch_candles(ticker1, start_date, end_date, interval='15')
        df2 = fetch_candles(ticker2, start_date, end_date, interval='15')
        if df1 is None or df2 is None or df1.empty or df2.empty:
            # Если 15-минутки не загружены, пробуем дневные
            logger.warning("15-минутные свечи не загружены, пробуем дневные")
            df1 = fetch_candles(ticker1, start_date, end_date, interval='1')
            df2 = fetch_candles(ticker2, start_date, end_date, interval='1')
            if df1 is None or df2 is None or df1.empty or df2.empty:
                logger.warning("Нет данных ни для одного из тикеров")
                return None
        # Объединяем по времени
        combined = pd.DataFrame({
            f'{ticker1}_close': df1['close'],
            f'{ticker2}_close': df2['close']
        }).dropna()
        if combined.empty:
            logger.warning("Нет общих временных меток после объединения")
            return None
        # Оставляем только часы торгов (с 7 до 19 МСК)
        combined = combined[combined.index.hour >= 7]
        if combined.empty:
            logger.warning("Нет данных в торговые часы")
            return None
        combined['Spread'] = combined[f'{ticker1}_close'] - combined[f'{ticker2}_close']
        logger.info(f"Спред построен, точек: {len(combined)}")
        return combined
    except Exception as e:
        logger.exception(f"Ошибка в get_spread_data: {e}")
        return None

# ======= ГЕНЕРАЦИЯ ГРАФИКА =======
def generate_spread_plot(pair_name):
    if pair_name not in PAIRS:
        return None, f"Пара '{pair_name}' не найдена."

    ticker1, ticker2 = PAIRS[pair_name]
    now = datetime.now()
    end_date = now.date() + timedelta(days=1)
    start_date = end_date - timedelta(days=DAYS_BACK)

    data = get_spread_data(ticker1, ticker2, start_date, end_date)
    if data is None or data.empty:
        return None, (f"Нет данных за последние {DAYS_BACK} дней по паре {pair_name}.\n"
                      f"Возможные причины: выходные дни, отсутствие торгов, или тикеры {ticker1}/{ticker2} не активны.\n"
                      f"Попробуйте позднее, когда будут доступны котировки.")

    fig, ax = plt.subplots(figsize=(12, 6))
    x = range(len(data))
    y = data['Spread'].values
    ax.plot(x, y, linewidth=1.8, label=f'Спред {ticker1} – {ticker2}')

    step = max(1, len(data) // 15)
    xticks_pos = list(range(0, len(data), step))
    xticks_labels = [data.index[i].strftime("%m-%d %H:%M") for i in xticks_pos]
    ax.set_xticks(xticks_pos)
    ax.set_xticklabels(xticks_labels, rotation=45, ha='right', fontsize=8)

    title_start = start_date.isoformat()
    title_end = (end_date - timedelta(days=1)).isoformat()
    ax.set_title(f'{pair_name} — спред ({ticker1} – {ticker2})\n{title_start} – {title_end}', fontsize=14)
    ax.set_ylabel('Разница (₽)')
    ax.set_xlabel('Время')
    ax.grid(True, alpha=0.3)
    ax.legend()

    last_spread = y[-1]
    last_time = data.index[-1]
    ax.text(1.02, 0.90,
            f'Текущий спред: {last_spread:.2f} ₽\n{last_time.strftime("%Y-%m-%d %H:%M")}',
            transform=ax.transAxes, verticalalignment='top', horizontalalignment='left',
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.8), fontsize=9)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf, None

# ======= ОТПРАВКА ГРАФИКА =======
async def send_spread_update(chat_id, pair_name, bot):
    try:
        logger.info(f"Генерация графика для {pair_name} для чата {chat_id}")
        buf, err = generate_spread_plot(pair_name)
        if err:
            logger.error(f"Ошибка генерации: {err}")
            await bot.send_message(chat_id=chat_id, text=f"❌ {err}")
            return
        await bot.send_photo(
            chat_id=chat_id,
            photo=buf,
            caption=f"📊 Спред {pair_name} ({PAIRS[pair_name][0]} – {PAIRS[pair_name][1]}) за последние {DAYS_BACK} дней"
        )
        logger.info(f"✅ График для {pair_name} отправлен")
    except Exception as e:
        logger.exception(f"❌ Критическая ошибка при отправке {pair_name}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Критическая ошибка для {pair_name}: {e}")

# ======= АВТООТПРАВКА =======
async def auto_send_all(bot):
    logger.info("Запуск автоматической отправки всех пар")
    for name in PAIRS:
        await send_spread_update(CHAT_ID, name, bot)
        await asyncio.sleep(3)
    logger.info("Автоотправка всех пар завершена")

# ======= КОМАНДЫ =======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /start от пользователя {user.id} (@{user.username})")
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    await update.message.reply_text(
        f"📈 Бот спредов российских акций.\n"
        f"Автоотправка всех пар каждые {UPDATE_INTERVAL_MINUTES} мин.\n\n"
        f"Доступные пары:\n" + "\n".join(f"• {n} ({t1}/{t2})" for n, (t1, t2) in PAIRS.items()),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /pairs от пользователя {user.id}")
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    await update.message.reply_text(
        "Выберите пару:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def spread_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /spread от пользователя {user.id}, пара по умолчанию: {DEFAULT_PAIR}")
    await update.message.reply_text(f"🔄 Загружаю {DEFAULT_PAIR}...")
    await send_spread_update(update.effective_chat.id, DEFAULT_PAIR, context.bot)
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pair_name = query.data.replace("pair_", "")
    user = update.effective_user
    logger.info(f"Callback от пользователя {user.id}: выбрана пара '{pair_name}'")
    if pair_name not in PAIRS:
        logger.warning(f"Неизвестная пара: {pair_name}")
        await query.edit_message_text("❌ Неизвестная пара")
        return
    await query.edit_message_text(f"⏳ Генерирую график для {pair_name}...")
    await send_spread_update(update.effective_chat.id, pair_name, context.bot)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Кнопки управления всегда под рукой 👇",
        reply_markup=MAIN_KEYBOARD
    )

async def handle_main_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    logger.info(f"Нажата Reply-кнопка: '{text}' от пользователя {user.id}")
    if text == "🏠 Старт":
        await start(update, context)
    elif text == "📊 Пары":
        await pairs_command(update, context)
    else:
        await update.message.reply_text("Используйте кнопки ниже:", reply_markup=MAIN_KEYBOARD)

# ======= ЗАПУСК =======
async def main():
    request = HTTPXRequest(read_timeout=30, write_timeout=30, connect_timeout=30, pool_timeout=30)
    application = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

    await application.bot.delete_webhook()
    logger.info("Вебхук удалён.")

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pairs", pairs_command))
    application.add_handler(CommandHandler("spread", spread_cmd))
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^pair_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_keyboard))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_send_all, 'interval', minutes=UPDATE_INTERVAL_MINUTES, args=[application.bot])
    scheduler.start()
    logger.info(f"Планировщик запущен, интервал {UPDATE_INTERVAL_MINUTES} мин.")

    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        bootstrap_retries=5,
        drop_pending_updates=True,
        allowed_updates=['message', 'callback_query', 'my_chat_member', 'chat_member']
    )

    logger.info(f"✅ Бот успешно запущен. Пары: {list(PAIRS.keys())}. Автоотправка каждые {UPDATE_INTERVAL_MINUTES} мин.")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка бота...")
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        scheduler.shutdown()
        # снимаем блокировку
        if os.name == "nt":
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        os.remove(LOCK_FILE)

if __name__ == "__main__":
    asyncio.run(main())