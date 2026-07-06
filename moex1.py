#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import io
import logging
import asyncio
import os
import signal
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

import pandas as pd
import matplotlib.pyplot as plt
from moexalgo import Ticker
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======= ПРОВЕРКА БЛОКИРОВКИ С PID =======
LOCK_FILE = "bot.lock"

def check_and_create_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                pid = int(f.read().strip())
            try:
                os.kill(pid, 0)
                print(f"❌ Бот уже запущен (PID {pid}). Завершите его и удалите {LOCK_FILE}.")
                sys.exit(1)
            except OSError:
                print(f"⚠️ Найден мёртвый PID {pid}, удаляю старую блокировку.")
                os.remove(LOCK_FILE)
        except (ValueError, IOError):
            os.remove(LOCK_FILE)
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    print(f"✅ Блокировка создана (PID {os.getpid()})")

check_and_create_lock()

# ======= НАСТРОЙКИ =======
TELEGRAM_TOKEN = "8885452009:AAFBmG8idkXGSs_TBA0n-_9GkiR1WmA1-_4"
CHAT_ID = 1234329121
UPDATE_INTERVAL_MINUTES = 60
DAYS_BACK = 14                     # увеличено для гарантии наличия данных

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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ======= ФУНКЦИЯ ЗАГРУЗКИ СПРЕДА С ТАЙМАУТОМ =======
async def get_spread_data(ticker1, ticker2, start_date, end_date):
    try:
        logger.info(f"Запрос данных для {ticker1} и {ticker2} с {start_date} по {end_date}")
        t1 = Ticker(ticker1)
        t2 = Ticker(ticker2)
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')

        intraday = True
        try:
            # Загружаем 15-минутные свечи с таймаутом 20 секунд
            data1 = await asyncio.wait_for(
                asyncio.to_thread(t1.candles, start=start_str, end=end_str, period='15min'),
                timeout=20.0
            )
            data2 = await asyncio.wait_for(
                asyncio.to_thread(t2.candles, start=start_str, end=end_str, period='15min'),
                timeout=20.0
            )
            logger.info(f"Загружены 15min свечи: {len(data1)} для {ticker1}, {len(data2)} для {ticker2}")
        except asyncio.TimeoutError:
            logger.warning(f"Таймаут загрузки 15min для {ticker1}/{ticker2}. Переход на дневные.")
            intraday = False
            data1 = await asyncio.wait_for(
                asyncio.to_thread(t1.candles, start=start_str, end=end_str, period='d'),
                timeout=15.0
            )
            data2 = await asyncio.wait_for(
                asyncio.to_thread(t2.candles, start=start_str, end=end_str, period='d'),
                timeout=15.0
            )
            logger.info(f"Загружены дневные свечи: {len(data1)} для {ticker1}, {len(data2)} для {ticker2}")
        except Exception as e:
            logger.exception(f"Ошибка загрузки свечей: {e}")
            return None

        if not data1 or not data2:
            logger.error("Данные пусты")
            return None

        df1 = pd.DataFrame(data1).set_index('begin')
        df2 = pd.DataFrame(data2).set_index('begin')
        df1.index = pd.to_datetime(df1.index)
        df2.index = pd.to_datetime(df2.index)

        combined = pd.DataFrame({
            f'{ticker1}_close': df1['close'],
            f'{ticker2}_close': df2['close']
        }).dropna()

        if intraday:
            combined = combined[combined.index.hour >= 7]

        if combined.empty:
            logger.warning("После фильтрации данных не осталось")
            return None

        combined['Spread'] = combined[f'{ticker1}_close'] - combined[f'{ticker2}_close']
        logger.info(f"Спред вычислен, {len(combined)} точек")
        return combined

    except Exception as e:
        logger.exception(f"Критическая ошибка в get_spread_data: {e}")
        return None

# ======= ГЕНЕРАЦИЯ ГРАФИКА (синхронная, но вызывается из асинхронной функции) =======
def generate_spread_plot_sync(pair_name):
    if pair_name not in PAIRS:
        return None, f"Пара '{pair_name}' не найдена."

    ticker1, ticker2 = PAIRS[pair_name]
    now = datetime.now()
    end_date = now.date() + timedelta(days=1)
    start_date = end_date - timedelta(days=DAYS_BACK)

    # Для простоты запускаем асинхронную функцию через asyncio.run() в синхронном контексте
    # Но лучше переделать всё на асинхрон, сейчас сделаем так:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        data = loop.run_until_complete(get_spread_data(ticker1, ticker2, start_date, end_date))
    finally:
        loop.close()

    if data is None or data.empty:
        return None, f"Нет данных за последние {DAYS_BACK} дней по паре {pair_name}.\nПроверьте тикеры {ticker1}/{ticker2}."

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

# ======= АСИНХРОННАЯ ОТПРАВКА ГРАФИКА =======
async def send_spread_update(chat_id, pair_name, bot):
    try:
        logger.info(f"Генерация графика для {pair_name} для чата {chat_id}")
        # Запускаем синхронную функцию в отдельном потоке, чтобы не блокировать event loop
        loop = asyncio.get_running_loop()
        buf, err = await loop.run_in_executor(None, generate_spread_plot_sync, pair_name)
        if err:
            await bot.send_message(chat_id=chat_id, text=f"❌ {err}")
            return
        await bot.send_photo(
            chat_id=chat_id,
            photo=buf,
            caption=f"📊 Спред {pair_name} ({PAIRS[pair_name][0]} – {PAIRS[pair_name][1]}) за последние {DAYS_BACK} дней"
        )
        logger.info(f"График для {pair_name} отправлен")
    except Exception as e:
        logger.exception(f"Ошибка при отправке {pair_name}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Критическая ошибка: {e}")

# ======= АВТООТПРАВКА =======
async def auto_send_all(bot):
    logger.info("Запуск автоматической отправки всех пар")
    for name in PAIRS:
        await send_spread_update(CHAT_ID, name, bot)
        await asyncio.sleep(3)
    logger.info("Автоотправка завершена")

# ======= КОМАНДЫ =======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /start от {user.id} (@{user.username})")
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    text = (f"📈 Бот спредов российских акций.\n"
            f"Автоотправка каждые {UPDATE_INTERVAL_MINUTES} мин.\n\n"
            f"Доступные пары:\n" + "\n".join(f"• {n} ({t1}/{t2})" for n, (t1, t2) in PAIRS.items()))
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /pairs от {user.id}")
    keyboard = [[InlineKeyboardButton(name, callback_data=f"pair_{name}")] for name in PAIRS]
    await update.message.reply_text("Выберите пару:", reply_markup=InlineKeyboardMarkup(keyboard))
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def spread_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Команда /spread от {user.id}, пара по умолчанию: {DEFAULT_PAIR}")
    await update.message.reply_text(f"🔄 Загружаю {DEFAULT_PAIR}...")
    await send_spread_update(update.effective_chat.id, DEFAULT_PAIR, context.bot)
    await update.message.reply_text("Кнопки управления всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pair_name = query.data.replace("pair_", "")
    user = update.effective_user
    logger.info(f"Callback от {user.id}: выбрана пара '{pair_name}'")
    if pair_name not in PAIRS:
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
    logger.info(f"Нажата Reply-кнопка: '{text}' от {user.id}")
    if text == "🏠 Старт":
        await start(update, context)
    elif text == "📊 Пары":
        await pairs_command(update, context)
    else:
        await update.message.reply_text("Используйте кнопки ниже:", reply_markup=MAIN_KEYBOARD)

# ======= ЗАПУСК =======
async def main():
    # Принудительно удаляем вебхук
    temp_app = Application.builder().token(TELEGRAM_TOKEN).build()
    await temp_app.bot.delete_webhook()
    logger.info("Вебхук удалён.")

    request = HTTPXRequest(read_timeout=30, write_timeout=30, connect_timeout=30, pool_timeout=30)
    application = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

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
        allowed_updates=['message', 'callback_query', 'my_chat_member', 'chat_member']
    )

    logger.info(f"✅ Бот запущен. Пары: {list(PAIRS.keys())}. Автоотправка каждые {UPDATE_INTERVAL_MINUTES} мин.")

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
        logger.info("Бот остановлен.")
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Прервано пользователем.")