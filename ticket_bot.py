import telebot
import time
import threading
import re
from datetime import datetime
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
import os
import logging
from fake_useragent import UserAgent

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logger = logging.getLogger('TicketBot')
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s')

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(formatter)

file_handler = logging.FileHandler('bot.log', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

# --- ЗАГРУЗКА .ENV ---
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))

if not TOKEN:
    logger.critical("❌ TOKEN не найден в .env. Выход.")
    exit(1)

bot = telebot.TeleBot(TOKEN, threaded=True)

# Инициализация UserAgent с обработкой ошибок
try:
    ua = UserAgent(browsers=['chrome', 'firefox', 'edge'])
    logger.info("✅ UserAgent успешно инициализирован")
except Exception as e:
    logger.warning(f"⚠️ Ошибка инициализации UserAgent: {e}. Используем запасной вариант.")
    ua = None

# --- ХРАНИЛИЩЕ ДАННЫХ И СОСТОЯНИЙ ---
active_jobs = {}  # {chat_id: {'thread': thread, 'stop_flag': False}}
user_steps = {}   # {chat_id: 'step_name'} - текущий шаг пользователя
user_data = {}    # {chat_id: {'from': ..., 'to': ..., 'date': ..., 'passengers': ...}}
heartbeat_enabled = set()  # Множество chat_id, у которых включен heartbeat

# Хранилище интервалов heartbeat: {chat_id: interval_seconds}
heartbeat_intervals = {}

# --- ФУНКЦИИ ПАРСИНГА ---

def get_headers():
    """Получение заголовков с случайным User-Agent"""
    if ua:
        try:
            return {
                "User-Agent": ua.random,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            }
        except Exception as e:
            logger.warning(f"⚠️ Ошибка получения User-Agent: {e}")
    
    # Запасной вариант
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

def get_trains_list(from_station, to_station, date):
    headers = get_headers()

    params = {"from": from_station, "to": to_station, "date": date}
    url = f"https://pass.rw.by/ru/route/?" + urlencode(params)

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
    except requests.Timeout:
        logger.error(f"⏱ Таймаут запроса к {url}")
        return []
    except requests.RequestException as e:
        logger.error(f"🌐 Ошибка запроса к {url}: {e}")
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    rows = soup.find_all('div', class_='sch-table__row-wrap')

    trains = []
    for row in rows:
        try:
            time_elem = row.find(class_='train-from-time')
            time_from = time_elem.get_text(strip=True) if time_elem else 'N/A'
            
            train_elem = row.find(class_='train-number')
            train_num = train_elem.get_text(strip=True) if train_elem else 'N/A'

            duration_elem = row.find(class_='train-duration-time') or row.find(class_='sch-table__duration')
            duration = duration_elem.get_text(strip=True) if duration_elem else 'N/A'

            status_cell = row.find(class_='cell-4')
            parsed_info = parse_carriage_info(status_cell) if status_cell else []

            trains.append({
                'time': time_from,
                'num': train_num,
                'duration': duration,
                'parsed_info': parsed_info
            })
        except Exception as e:
            logger.warning(f"⚠️ Пропущена строка поезда: {e}")
            continue

    logger.debug(f"✅ Найдено {len(trains)} поездов")
    return trains

def parse_carriage_info(status_cell):
    carriages = []
    items = status_cell.find_all('div', class_='sch-table__t-item')

    for item in items:
        try:
            type_elem = item.find('div', class_='sch-table__t-name')
            carriage_type = type_elem.get_text(strip=True) if type_elem else "Неизвестный"

            quant_elem = item.find('a', class_='sch-table__t-quant')
            seats_span = quant_elem.find('span') if quant_elem else None
            seats_raw = seats_span.get_text(strip=True) if seats_span else "?"
            
            # Очищаем количество мест (убираем текст "мест", оставляем цифры или ставим 0)
            seats = "0"
            if seats_raw != "?":
                match = re.search(r'\d+', seats_raw)
                if match:
                    seats = match.group()
                else:
                    seats = "0"

            price_elem = item.find('span', class_='js-price')
            price_byn = price_elem.get('data-cost-byn') if price_elem else None
            if not price_byn:
                cost_span = item.find('span', class_='ticket-cost')
                price_byn = cost_span.get_text(strip=True) if cost_span else "?"

            carriages.append({
                'type': carriage_type,
                'seats': seats,
                'price_byn': price_byn
            })
        except Exception as e:
            logger.warning(f"⚠️ Ошибка парсинга вагона: {e}")
            continue

    return carriages

# --- ФУНКЦИИ ОТСЛЕЖИВАНИЯ ---

def tracking_worker(chat_id, from_station, to_station, date, selected_time):
    logger.info(f"🔄 Запуск трекинга: {chat_id} | {selected_time}")
    num_passengers = user_data[chat_id].get('passengers', 1)
    last_heartbeat = time.time()
    # Получаем интервал heartbeat для этого пользователя (по умолчанию 1800 сек = 30 мин)
    hb_interval = heartbeat_intervals.get(chat_id, 1800)

    while chat_id in active_jobs:
        try:
            # Отправка heartbeat сообщения если включено и прошел заданный интервал
            if chat_id in heartbeat_enabled and (time.time() - last_heartbeat) >= hb_interval:
                logger.debug(f"💓 Heartbeat для {chat_id} (интервал: {hb_interval} сек)")
                bot.send_message(chat_id, "💓 Бот работает, проверяю билеты...")
                last_heartbeat = time.time()

            trains = get_trains_list(from_station, to_station, date)
            current_train = next((t for t in trains if t['time'] == selected_time), None)

            if not current_train:
                logger.warning(f"Поезд {selected_time} не найден в списке.")
                time.sleep(CHECK_INTERVAL)
                continue

            suitable = [
                c for c in current_train['parsed_info']
                if c['seats'].isdigit() and int(c['seats']) >= num_passengers
            ]

            if suitable:
                msg = f"🎉 <b>УСПЕХ!</b> Места для {num_passengers} чел. в поезде {selected_time} появились!\n\n"
                msg += f"📍 {from_station} → {to_station}\n📅 {date}"
                bot.send_message(chat_id, msg, parse_mode="HTML")
                send_detailed_train_info(chat_id, current_train, num_passengers)
                
                active_jobs.pop(chat_id, None)
                heartbeat_enabled.discard(chat_id)  # Убираем из heartbeat при успехе
                heartbeat_intervals.pop(chat_id, None)  # Очищаем интервал при успехе
                logger.info(f"✅ Трекинг завершен успешно для {chat_id}")
                return

            logger.debug(f"[{datetime.now().strftime('%H:%M')}] Нет мест для {chat_id}. Ждем...")
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"Ошибка в потоке трекинга {chat_id}: {e}")
            time.sleep(CHECK_INTERVAL)

def send_detailed_train_info(chat_id, train, num_passengers=None):
    lines = [
        f"🚂 <b>Поезд №{train['num']}</b>",
        f"⏱ Отправление: {train['time']}",
        f"⏳ В пути: {train['duration']}",
        f"👥 Нужно мест: {num_passengers}",
        "------------------",
        "<b>Доступные вагоны:</b>"
    ]

    for c in train['parsed_info']:
        seats_int = int(c['seats']) if c['seats'].isdigit() else 0
        is_enough = seats_int >= num_passengers if num_passengers else False
        
        status = "✅" if is_enough else "❌"
        icon = "🪑" if "Сидячий" in c['type'] else "🛏"
        
        lines.append(f"{status} {icon} <b>{c['type']}</b>: {c['seats']} мест ({c['price_byn']} BYN)")

    full_text = "\n".join(lines)
    bot.send_message(chat_id, full_text, parse_mode="HTML")

# --- МАШИНА СОСТОЯНИЙ (ПОШАГОВЫЙ ВВОД) ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_steps.pop(message.chat.id, None)
    user_data.pop(message.chat.id, None)
    
    text = (
        "👋 Привет! Я бот для отслеживания билетов БЖД.\n\n"
        "Я помогу найти места в нужном поезде.\n"
        "Нажми /track, чтобы начать поиск."
    )
    bot.reply_to(message, text)

@bot.message_handler(commands=['track'])
def start_track(message):
    chat_id = message.chat.id
    user_steps[chat_id] = 'ask_from'
    user_data[chat_id] = {}
    
    bot.send_message(
        chat_id, 
        "1️⃣ <b>Откуда едем?</b>\nНапишите название станции отправления (например: <i>Минск</i>)",
        parse_mode="HTML"
    )
    logger.info(f"Начат трек для {chat_id}. Шаг: ask_from")

@bot.message_handler(commands=['stop'])
def stop_tracking_cmd(message):
    chat_id = message.chat.id
    if chat_id in active_jobs:
        active_jobs.pop(chat_id)
        bot.reply_to(message, "⏹ Отслеживание остановлено.")
    else:
        bot.reply_to(message, "ℹ️ У вас нет активных задач.")
    user_steps.pop(chat_id, None)

@bot.message_handler(func=lambda message: message.chat.id in user_steps)
def handle_step_input(message):
    chat_id = message.chat.id
    current_step = user_steps[chat_id]
    text = message.text.strip()

    if current_step == 'ask_from':
        user_data[chat_id]['from'] = text
        user_steps[chat_id] = 'ask_to'
        bot.send_message(
            chat_id,
            f"2️⃣ <b>Куда едем?</b>\nВы указали: <i>{text}</i>\nТеперь напишите станцию назначения:",
            parse_mode="HTML"
        )

    elif current_step == 'ask_to':
        user_data[chat_id]['to'] = text
        user_steps[chat_id] = 'ask_date'
        bot.send_message(
            chat_id,
            f"3️⃣ <b>Дата поездки?</b>\nМаршрут: <i>{user_data[chat_id]['from']} → {text}</i>\nВведите дату в формате ГГГГ-ММ-ДД (например: 2026-05-20):",
            parse_mode="HTML"
        )

    elif current_step == 'ask_date':
        if not re.match(r"\d{4}-\d{2}-\d{2}", text):
            bot.send_message(chat_id, "❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД (например: 2026-05-20).\nПопробуйте еще раз:")
            return
        
        user_data[chat_id]['date'] = text
        user_steps[chat_id] = 'ask_passengers'
        bot.send_message(
            chat_id,
            f"4️⃣ <b>Сколько пассажиров?</b>\nДата: <i>{text}</i>\nВведите число (1, 2, 3...):",
            parse_mode="HTML"
        )

    elif current_step == 'ask_passengers':
        if not text.isdigit() or int(text) <= 0:
            bot.send_message(chat_id, "❌ Введите корректное число пассажиров (больше 0):")
            return
        
        num_pax = int(text)
        user_data[chat_id]['passengers'] = num_pax
        user_steps.pop(chat_id, None)

        loading_msg = bot.send_message(chat_id, f"🔍 Ищу поезда по маршруту {user_data[chat_id]['from']} → {user_data[chat_id]['to']} на {user_data[chat_id]['date']}...")
        
        trains = get_trains_list(user_data[chat_id]['from'], user_data[chat_id]['to'], user_data[chat_id]['date'])
        
        bot.delete_message(chat_id, loading_msg.message_id)

        if not trains:
            bot.send_message(chat_id, "❌ К сожалению, поездов не найдено. Проверьте названия станций или дату.\nПопробуйте /track заново.")
            return

        show_train_list(chat_id, trains)

def show_train_list(chat_id, trains):
    keyboard = InlineKeyboardMarkup(row_width=1)
    pax = user_data[chat_id]['passengers']

    for train in trains:
        has_enough = any(
            c['seats'].isdigit() and int(c['seats']) >= pax
            for c in train['parsed_info']
        )
        status_emoji = "✅" if has_enough else "❌"
        
        summary_parts = []
        for c in train['parsed_info'][:2]:
            summary_parts.append(f"{c['type']}: {c['seats']}")
        if len(train['parsed_info']) > 2:
            summary_parts.append("...")
        
        summary = ", ".join(summary_parts) if summary_parts else "Нет данных"
        
        btn_text = f"{status_emoji} {train['time']} | №{train['num']} | {summary}"
        if len(btn_text) > 60:
            btn_text = btn_text[:57] + "..."

        keyboard.add(InlineKeyboardButton(
            text=btn_text,
            callback_data=f"preview_{train['time']}_{train['num']}"
        ))

    bot.send_message(chat_id, f"📋 Найдено поездов: {len(trains)}. Выберите нужный для деталей:", reply_markup=keyboard)

# --- ОБРАБОТКА НАЖАТИЙ (CALLBACKS) ---

@bot.callback_query_handler(func=lambda call: call.data.startswith("preview_"))
def on_preview(call):
    chat_id = call.message.chat.id
    data = call.data.split("_", 2)
    if len(data) < 3:
        return
    
    sel_time, sel_num = data[1], data[2]
    info = user_data.get(chat_id)
    
    if not info:
        bot.answer_callback_query(call.id, "Сессия истекла. Начните /track", show_alert=True)
        return

    trains = get_trains_list(info['from'], info['to'], info['date'])
    train = next((t for t in trains if t['time'] == sel_time and t['num'] == sel_num), None)

    if not train:
        bot.answer_callback_query(call.id, "Поезд не найден (обновите список)", show_alert=True)
        return

    send_detailed_train_info(chat_id, train, info['passengers'])

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("▶️ Запустить мониторинг", callback_data=f"confirm_{sel_time}_{sel_num}"))
    kb.add(InlineKeyboardButton("🔙 Назад к списку", callback_data="back_to_list"))
    
    bot.send_message(chat_id, f"Запустить непрерывную проверку для поезда №{sel_num} ({sel_time})?", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_"))
def on_confirm(call):
    chat_id = call.message.chat.id
    data = call.data.split("_", 2)
    sel_time, sel_num = data[1], data[2]
    
    if chat_id in active_jobs:
        bot.answer_callback_query(call.id, "У вас уже есть активный мониторинг!", show_alert=True)
        return

    info = user_data.get(chat_id)
    if not info:
        bot.answer_callback_query(call.id, "Ошибка сессии", show_alert=True)
        return

    # Предлагаем выбрать интервал heartbeat
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("10 мин", callback_data=f"hb_interval_600_{sel_time}_{sel_num}"),
        InlineKeyboardButton("20 мин", callback_data=f"hb_interval_1200_{sel_time}_{sel_num}")
    )
    kb.add(
        InlineKeyboardButton("30 мин", callback_data=f"hb_interval_1800_{sel_time}_{sel_num}"),
        InlineKeyboardButton("1 час", callback_data=f"hb_interval_3600_{sel_time}_{sel_num}")
    )
    kb.add(InlineKeyboardButton("❌ Без heartbeat", callback_data=f"heartbeat_no_{sel_time}_{sel_num}"))
    
    bot.send_message(chat_id, "💓 Выберите интервал сообщений 'Бот работает':", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("hb_interval_") or call.data.startswith("heartbeat_no_"))
def on_heartbeat_choice(call):
    chat_id = call.message.chat.id
    data = call.data.split("_", 4)  # hb_interval/heartbeat_no + interval_or_empty + sel_time + sel_num
    
    if len(data) < 4:
        bot.answer_callback_query(call.id, "Ошибка формата", show_alert=True)
        return
    
    choice_type = data[0]  # hb_interval или heartbeat_no
    
    if choice_type == "hb_interval":
        # Формат: hb_interval_600_seltime_selnun
        interval_seconds = int(data[1])
        sel_time, sel_num = data[2], data[3]
        
        info = user_data.get(chat_id)
        if not info:
            bot.answer_callback_query(call.id, "Ошибка сессии", show_alert=True)
            return
        
        # Сохраняем интервал и включаем heartbeat
        heartbeat_intervals[chat_id] = interval_seconds
        heartbeat_enabled.add(chat_id)
        
        minutes = interval_seconds // 60
        if minutes >= 60:
            hours = minutes // 60
            msg_text = f"✅ Heartbeat включен! Сообщения каждые {hours} ч."
        else:
            msg_text = f"✅ Heartbeat включен! Сообщения каждые {minutes} мин."
        
        bot.answer_callback_query(call.id, msg_text, show_alert=False)
        bot.send_message(chat_id, f"💓 {msg_text} Мониторинг запущен.")
        
    else:  # heartbeat_no
        sel_time, sel_num = data[1], data[2]
        
        info = user_data.get(chat_id)
        if not info:
            bot.answer_callback_query(call.id, "Ошибка сессии", show_alert=True)
            return
        
        # Отключаем heartbeat
        heartbeat_enabled.discard(chat_id)
        heartbeat_intervals.pop(chat_id, None)
        bot.answer_callback_query(call.id, "✅ Мониторинг запущен без heartbeat.", show_alert=False)
    
    # Запуск трекинга (для обоих случаев)
    if choice_type == "hb_interval" or choice_type == "heartbeat_no":
        if choice_type == "heartbeat_no":
            sel_time, sel_num = data[1], data[2]
        else:
            sel_time, sel_num = data[2], data[3]
        
        info = user_data.get(chat_id)
        if not info:
            bot.answer_callback_query(call.id, "Ошибка сессии", show_alert=True)
            return
        
        thread = threading.Thread(
            target=tracking_worker,
            args=(chat_id, info['from'], info['to'], info['date'], sel_time),
            daemon=True
        )
        thread.start()
        active_jobs[chat_id] = {'thread': thread, 'stop_flag': False}
        
        hb_status = "включен" if chat_id in heartbeat_enabled else "выключен"
        logger.info(f"Мониторинг активирован: {chat_id} -> {sel_time} | heartbeat={hb_status}")

@bot.callback_query_handler(func=lambda call: call.data == "back_to_list")
def on_back(call):
    chat_id = call.message.chat.id
    info = user_data.get(chat_id)
    if not info:
        bot.send_message(chat_id, "Сессия утеряна. Используйте /track")
        return

    bot.answer_callback_query(call.id, "Обновляю список...")
    trains = get_trains_list(info['from'], info['to'], info['date'])
    
    if not trains:
        bot.send_message(chat_id, "Список пуст.")
        return

    show_train_list(chat_id, trains)

# --- ЗАПУСК ---
if __name__ == '__main__':
    logger.info("🚀 Бот запущен в пошаговом режиме.")
    try:
        bot.polling(none_stop=True)
    except KeyboardInterrupt:
        logger.info("🛑 Остановка бота.")
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка: {e}", exc_info=True)
