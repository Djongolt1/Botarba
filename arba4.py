#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import io
import logging
import asyncio
import os
from datetime import datetime, timedelta

# ===== АВТОУСТАНОВКА НЕДОСТАЮЩИХ БИБЛИОТЕК =====
required_packages = [
    "matplotlib",
    "moexalgo",
    "pandas",
    "python-telegram-bot[job-queue]",
    "apscheduler"
]

for package in required_packages:
    try:
        if package.startswith("python-telegram-bot"):
            import telegram
        elif package == "moexalgo":
            import moexalgo
        elif package == "pandas":
            import pandas
        elif package == "matplotlib":
            import matplotlib
        elif package == "apscheduler":
            import apscheduler
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package.split("[")[0]])

# Теперь импортируем всё необходимое
import pandas as pd
import matplotlib.pyplot as plt
from moexalgo import Ticker
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======= ПРОВЕРКА БЛОКИРОВКИ (ОДИН ЭКЗЕМПЛЯР) =======
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
        print("❌ Бот уже запущен! Завершите предыдущий процесс и удалите bot.lock")
        sys.exit(1)

# ======= НАСТРОЙКИ =======
TELEGRAM_TOKEN = "8663503148:AAEr1Vss6v1vLHf47u1M-Vp_wbhIn0PLBd8"
CHAT_ID = 1234329121               # ID чата (число)
UPDATE_INTERVAL_MINUTES = 60       # как часто слать автоматические отчёты (для всех пар)
DAYS_BACK = 5                      # период в днях для всех пар

# Список пар для отображения
PAIRS = {
    "Мечел": ("MTLR", "MTLRP"),
    "Татнефть": ("TATN", "TATNP"),
    "Ростелеком": ("RTKM", "RTKMP"),
    "Сбер": ("SBER", "SBERP"),
}
DEFAULT_PAIR = "Мечел"

# ======= ПОСТОЯННАЯ КЛАВИАТУРА =======
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📊 Пары"), KeyboardButton("🏠 Главное меню")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)

# ======= ЛОГИРОВАНИЕ =======
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ======= ФУНКЦИЯ ЗАГРУЗКИ СПРЕДА =======
def get_spread_data(ticker1, ticker2, start_date, end_date):
    """Возвращает DataFrame с колонками close1, close2 и Spread"""
    try:
        t1 = Ticker(ticker1)
        t2 = Ticker(ticker2)
        # Пытаемся получить 15-минутные свечи, если не получается – дневные
        try:
            data1 = t1.candles(start=start_date, end=end_date, period='15min')
            data2 = t2.candles(start=start_date, end=end_date, period='15min')
            logger.info(f"Загружены 15min свечи для {ticker1} и {ticker2}")
        except Exception as e:
            logger.warning(f"Не удалось загрузить 15min свечи: {e}, пробуем дневные")
            data1 = t1.candles(start=start_date, end=end_date)
            data2 = t2.candles(start=start_date, end=end_date)

        df1 = pd.DataFrame(data1).set_index('begin')
        df2 = pd.DataFrame(data2).set_index('begin')
        df1.index = pd.to_datetime(df1.index)
        df2.index = pd.to_datetime(df2.index)

        if df1.empty or df2.empty:
            logger.error(f"Нет данных для {ticker1} или {ticker2}")
            return None

        combined = pd.DataFrame({
            f'{ticker1}_close': df1['close'],
            f'{ticker2}_close': df2['close']
        }).dropna()
        combined['Spread'] = combined[f'{ticker1}_close'] - combined[f'{ticker2}_close']
        # оставляем только с 7:00 утра (актуально для внутридневных данных)
        combined = combined[combined.index.hour >= 7]
        if combined.empty:
            logger.warning(f"Нет данных после 7:00 для {ticker1}/{ticker2}")
            return None
        return combined
    except Exception as e:
        logger.error(f"Ошибка загрузки {ticker1}/{ticker2}: {e}")
        return None

# ======= ГЕНЕРАЦИЯ ГРАФИКА ДЛЯ ЛЮБОЙ ПАРЫ =======
def generate_spread_plot(pair_name, days_back=DAYS_BACK):
    """Создаёт график спреда для указанной пары.
       Возвращает (BytesIO, ошибка)"""
    if pair_name not in PAIRS:
        return None, f"Пара '{pair_name}' не найдена."

    ticker1, ticker2 = PAIRS[pair_name]
    today = datetime.now().date()
    start_date = today - timedelta(days=days_back)
    end_date = today

    data = get_spread_data(ticker1, ticker2, start_date, end_date)
    if data is None or data.empty:
        return None, f"Не удалось загрузить данные по паре {pair_name} ({ticker1}/{ticker2}).\nПроверьте, торгуются ли инструменты в выбранный период."

    # Строим график
    fig, ax = plt.subplots(figsize=(12, 6))
    x = range(len(data))
    y = data['Spread'].values
    ax.plot(x, y, linewidth=1.8, label=f'Спред {ticker1} – {ticker2}')

    # Настройка подписей оси X
    step = max(1, len(data) // 15)
    xticks_pos = list(range(0, len(data), step))
    xticks_labels = [data.index[i].strftime("%m-%d %H:%M") for i in xticks_pos]
    ax.set_xticks(xticks_pos)
    ax.set_xticklabels(xticks_labels, rotation=45, ha='right', fontsize=8)

    ax.set_title(f'{pair_name} — спред ({ticker1} – {ticker2})\n{start_date} – {today}', fontsize=14)
    ax.set_ylabel('Разница (₽)')
    ax.set_xlabel('Время (порядок свечей)')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='upper left')

    # Текущее значение спреда
    last_spread = y[-1]
    last_time = data.index[-1]
    ax.text(1.02, 0.90,
            f'Текущий спред: {last_spread:.2f} ₽\nВремя расчёта:\n{last_time.strftime("%Y-%m-%d %H:%M")}',
            transform=ax.transAxes, verticalalignment='top', horizontalalignment='left',
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.8), fontsize=9)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf, None

# ======= АСИНХРОННАЯ ОТПРАВКА ГРАФИКА ДЛЯ ОДНОЙ ПАРЫ =======
async def send_spread_update(chat_id, pair_name, bot, retries=3):
    """Генерирует и отправляет график для заданной пары с повторными попытками"""
    for attempt in range(retries):
        logger.info(f"Генерация отчёта по паре {pair_name} (попытка {attempt+1}/{retries})...")
        buf, err = generate_spread_plot(pair_name)
        if err:
            logger.error(err)
            if attempt == retries - 1:
                await bot.send_message(chat_id=chat_id, text=f"❌ Ошибка: {err}")
            else:
                await asyncio.sleep(5)
            continue

        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=buf,
                caption=f"📊 Спред {pair_name} ({PAIRS[pair_name][0]} – {PAIRS[pair_name][1]}) за последние {DAYS_BACK} дней"
            )
            return
        except Exception as e:
            logger.error(f"Ошибка отправки фото: {e}")
            if attempt == retries - 1:
                await bot.send_message(chat_id=chat_id, text=f"❌ Не удалось отправить график для {pair_name}: {e}")
            else:
                await asyncio.sleep(5)

# ======= АВТОМАТИЧЕСКАЯ ОТПРАВКА ДЛЯ ВСЕХ ПАР =======
async def auto_send_all(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет графики всех пар в CHAT_ID"""
    bot = context.bot
    for pair_name in PAIRS.keys():
        await send_spread_update(CHAT_ID, pair_name, bot)
        await asyncio.sleep(3)

# ======= КОМАНДЫ БОТА =======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(pair, callback_data=f"pair_{pair}")] for pair in PAIRS.keys()
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Привет! Я бот для отслеживания спреда российских акций.\n\n"
        f"Выбери пару на кнопках ниже, или используй команду /pairs.\n"
        f"Автоматическая отправка всех пар происходит каждые {UPDATE_INTERVAL_MINUTES} минут.\n\n"
        f"Доступные пары:\n" + "\n".join(f"• {name} ({t1}/{t2})" for name, (t1, t2) in PAIRS.items()),
        reply_markup=reply_markup
    )
    # Отправляем постоянную клавиатуру
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(pair, callback_data=f"pair_{pair}")] for pair in PAIRS.keys()
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Выбери пару для получения графика спреда:",
        reply_markup=reply_markup
    )
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def spread_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Генерирую график для пары {DEFAULT_PAIR}...")
    await send_spread_update(update.effective_chat.id, DEFAULT_PAIR, context.bot)
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pair_name = query.data.replace("pair_", "")
    if pair_name not in PAIRS:
        await query.edit_message_text(f"❌ Неизвестная пара: {pair_name}")
        return
    await query.edit_message_text(f"🔄 Генерирую график для пары {pair_name}...")
    await send_spread_update(update.effective_chat.id, pair_name, context.bot)
    # После отправки графика клавиатура сбросилась, напомним
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

# Обработчик текстовых кнопок
async def handle_main_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Пары":
        await pairs_command(update, context)
    elif text == "🏠 Главное меню":
        await start(update, context)
    else:
        # Неизвестная команда – показываем клавиатуру
        await update.message.reply_text("Используй кнопки ниже:", reply_markup=MAIN_KEYBOARD)

# ======= ЗАПУСК БОТА =======
async def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    async with application:
        await application.bot.delete_webhook()
        logger.info("Webhook удалён, работаем через polling.")

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pairs", pairs_command))
    application.add_handler(CommandHandler("spread", spread_cmd))
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^pair_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_keyboard))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_send_all, 'interval', minutes=UPDATE_INTERVAL_MINUTES, args=[application.bot])
    scheduler.start()

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    logger.info(f"✅ Бот запущен. Пары: {list(PAIRS.keys())}. Автоотправка всех пар каждые {UPDATE_INTERVAL_MINUTES} мин.")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка бота...")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        scheduler.shutdown()
        if os.name == "nt":
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        os.remove(LOCK_FILE)

if __name__ == "__main__":
    asyncio.run(main())