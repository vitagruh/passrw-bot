import telebot
import time
import threading
from datetime import datetime
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
import os
import re
import fake_useragent
import logging
import json
import signal
import sys

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logger = logging.getLogger('TicketBotLogger')
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

file_handler = logging.FileHandler('bot.log', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# --- ЗАГРУЗКА .ENV ---
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
JOBS_FILE = os.getenv("JOBS_FILE", "active_jobs.json")

if not TOKEN:
    logger.critical("TOKEN не найден в .env файле. Выход.")
    exit()

bot = telebot.TeleBot(TOKEN)
ua = fake_useragent.UserAgent()

# --- ХРАНИЛИЩЕ ---
active_jobs = {}  # {'chat_id': {'thread': thread, 'data': {...}}, ...}
user_data = {}  # {'chat_id': {...}, ...}

# --- ПЕРСИСТЕНТНОСТЬ ---

def load_active_jobs():
    """Загружает активные задачи из файла при старте"""
    global active_jobs
    if os.path.exists(JOBS_FILE):
        try:
            with open(JOBS_FILE, 'r', encoding='utf-8') as f:
                jobs = json.load(f)
            for chat_id_str, job_data in jobs.items():
                chat_id = int(chat_id_str)
                user_data[chat_id] = job_data.get('user_data', {})
                
                # Перезапускаем поток для каждой задачи
                thread = threading.Thread(
                    target=tracking_worker,
                    args=(
                        chat_id,
                        job_data['from'],
                        job_data['to'],
                        job_data['date'],
                        job_data['selected_time']
                    ),
                    daemon=True
                )
                thread.start()
                active_jobs[chat_id] = {'thread': thread, 'data': job_data}
            logger.info(f"Загружено {len(active_jobs)} активных задач из файла")
        except Exception as e:
            logger.error(f"Ошибка при загрузке активных задач: {e}")

def save_active_jobs():
    """Сохраняет активные задачи в файл"""
    try:
        jobs_to_save = {}
        for chat_id, job_info in active_jobs.items():
            data = job_info['data']
            jobs_to_save[str(chat_id)] = data
        
        with open(JOBS_FILE, 'w', encoding='utf-8') as f:
            json.dump(jobs_to_save, f, ensure_ascii=False, indent=2)
        logger.debug("Активные задачи сохранены")
    except Exception as e:
        logger.error(f"Ошибка при сохранении активных задач: {e}")

def remove_job_from_file(chat_id):
    """Удаляет задачу из файла после завершения"""
    if os.path.exists(JOBS_FILE):
        try:
            with open(JOBS_FILE, 'r', encoding='utf-8') as f:
                jobs = json.load(f)
            
            if str(chat_id) in jobs:
                del jobs[str(chat_id)]
                with open(JOBS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(jobs, f, ensure_ascii=False, indent=2)
                logger.debug(f"Задача {chat_id} удалена из файла")
        except Exception as e:
            logger.error(f"Ошибка при удалении задачи из файла: {e}")

# --- ФУНКЦИИ ПАРСИНГА ---

def get_trains_list(from_station, to_station, date):
    headers = {
        "User-Agent": ua.random,
        "Accept": "text/html,application/xhtml+xml,*/*",
    }

    params = {
        "from": from_station,
        "to": to_station,
        "date": date,
    }
    url = f"https://pass.rw.by/ru/route/?" + urlencode(params)

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.Timeout:
        logger.error(f"Timeout при запросе к {url}")
        return []
    except requests.RequestException as e:
        logger.error(f"Ошибка при запросе к {url}: {e}")
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    rows = soup.find_all('div', class_='sch-table__row-wrap')

    trains = []
    for row in rows:
        try:
            time_elem = row.find(class_='train-from-time')
            time_from = time_elem.get_text(strip=True) if time_elem else 'N/A'
            
            train_num_elem = row.find(class_='train-number')
            train_num = train_num_elem.get_text(strip=True) if train_num_elem else 'N/A'

            duration_elem = row.find(class_='train-duration-time') or row.find(class_='sch-table__duration')
            duration = duration_elem.get_text(strip=True) if duration_elem else 'N/A'

            status_cell = row.find(class_='cell-4')
            if not status_cell:
                parsed_info = []
            else:
                parsed_info = parse_carriage_info(status_cell)

            trains.append({
                'time': time_from,
                'num': train_num,
                'duration': duration,
                'parsed_info': parsed_info,
                'raw_row': row
            })
        except AttributeError as e:
            logger.warning(f"Ошибка при парсинге строки поезда: {e}")
            continue

    logger.debug(f"Получено {len(trains)} поездов для {from_station} - {to_station} ({date})")
    return trains

def parse_carriage_info(status_cell):
    """
    Парсит блок с местами и ценами (cell-4) и возвращает список вагонов.
    """
    carriages = []
    items = status_cell.find_all('div', class_='sch-table__t-item')

    for item in items:
        type_elem = item.find('div', class_='sch-table__t-name')
        carriage_type = type_elem.get_text(strip=True) if type_elem else "Неизвестный"

        quant_elem = item.find('a', class_='sch-table__t-quant')
        seats = "?"
        if quant_elem:
            seats_span = quant_elem.find('span')
            if seats_span:
                seats_text = seats_span.get_text(strip=True)
                # Извлекаем только цифры из текста (например, "5 мест" -> 5)
                numbers = re.findall(r'\d+', seats_text)
                if numbers:
                    seats = numbers[0]
                else:
                    seats = seats_text if seats_text else "?"

        price_byn = "?"
        price_elem = item.find('span', class_='js-price')
        if price_elem:
            price_byn = price_elem.get('data-cost-byn')
            if not price_byn:
                cost_span = item.find('span', class_='ticket-cost')
                if cost_span:
                    price_text = cost_span.get_text(strip=True)
                    numbers = re.findall(r'\d+\.?\d*', price_text)
                    if numbers:
                        price_byn = numbers[0]

        carriages.append({
            'type': carriage_type,
            'seats': seats,
            'price_byn': price_byn
        })

    logger.debug(f"Разобрано {len(carriages)} вагонов из cell-4.")
    return carriages

# --- ФУНКЦИИ ОТСЛЕЖИВАНИЯ ---

def tracking_worker(chat_id, from_station, to_station, date, selected_time):
    logger.info(f"Начато отслеживание для {chat_id} - {from_station} -> {to_station}, {date}, {selected_time}")
    num_passengers_needed = user_data.get(chat_id, {}).get('passengers', 1)
    
    check_count = 0
    max_checks_without_response = 5  # Максимальное количество неудачных проверок подряд

    while True:
        try:
            trains = get_trains_list(from_station, to_station, date)
            
            if not trains:
                check_count += 1
                if check_count >= max_checks_without_response:
                    logger.warning(f"Нет данных о поездах {max_checks_without_response} раз подряд для {chat_id}. Отправка уведомления.")
                    try:
                        bot.send_message(
                            chat_id, 
                            f"⚠️ Не удалось получить данные о поездах несколько раз подряд. "
                            f"Возможно, сайт недоступен или изменилась структура. "
                            f"Отслеживание остановлено."
                        )
                    except:
                        pass
                    active_jobs.pop(chat_id, None)
                    remove_job_from_file(chat_id)
                    return
                logger.warning(f"Пустой список поездов для {chat_id}. Попытка {check_count}/{max_checks_without_response}")
                time.sleep(CHECK_INTERVAL)
                continue
            
            check_count = 0  # Сбрасываем счётчик при успешном получении данных
            
            current_train = next((t for t in trains if t['time'] == selected_time), None)

            if not current_train:
                logger.warning(f"Не удалось найти поезд в {selected_time} для {chat_id}. Возможно, поезд снят с маршрута.")
                try:
                    bot.send_message(
                        chat_id, 
                        f"❌ Поезд в {selected_time} больше не найден в расписании. "
                        f"Возможно, он был отменён или изменилось время отправления. "
                        f"Отслеживание остановлено."
                    )
                except:
                    logger.error(f"Не удалось отправить сообщение пользователю {chat_id}")
                active_jobs.pop(chat_id, None)
                remove_job_from_file(chat_id)
                return

            parsed_info = current_train['parsed_info']
            
            # Проверяем наличие мест с улучшенной обработкой
            suitable_carriages = []
            for c in parsed_info:
                seats_str = c['seats']
                if seats_str != '?' and seats_str.isdigit():
                    seats_int = int(seats_str)
                    if seats_int >= num_passengers_needed:
                        suitable_carriages.append(c)

            if suitable_carriages:
                logger.info(f"НАЙДЕНО МЕСТО! Поезд {selected_time} для {chat_id}. Отправка уведомления.")
                try:
                    bot.send_message(
                        chat_id, 
                        f"🎉 УСПЕХ: Поезд в {selected_time} — места для {num_passengers_needed} пассажиров доступны!\n\n"
                        f"Рекомендуем быстро оформить заказ на сайте pass.rw.by"
                    )
                    send_detailed_train_info(chat_id, current_train, num_passengers_needed)
                except Exception as e:
                    logger.error(f"Ошибка при отправке уведомления о успехе: {e}")
                
                active_jobs.pop(chat_id, None)
                remove_job_from_file(chat_id)
                logger.info(f"Отслеживание для {chat_id} завершено успешно.")
                return
            else:
                current_time = datetime.now().strftime("%H:%M:%S")
                log_msg = f"[{current_time}] Нет подходящих мест для {num_passengers_needed} пасс. в поезде {selected_time} (ID: {chat_id}). Повтор через {CHECK_INTERVAL}s."
                logger.info(log_msg)
                
        except Exception as e:
            logger.error(f"Ошибка в потоке отслеживания для {chat_id}: {e}")
            check_count += 1
            if check_count >= max_checks_without_response:
                try:
                    bot.send_message(chat_id, f"❌ Произошла ошибка при отслеживании. Отслеживание остановлено.")
                except:
                    pass
                active_jobs.pop(chat_id, None)
                remove_job_from_file(chat_id)
                return
        
        time.sleep(CHECK_INTERVAL)

def send_detailed_train_info(chat_id, train, num_passengers_needed=None):
    lines = [
        f"🚂 Поезд: {train['num']}",
        f"⏱ Отправление: {train['time']}",
        f"⏱ Время в пути: {train['duration']}",
        f"📅 Дата: {user_data.get(chat_id, {}).get('date', 'N/A')}",
        f"📍 {user_data.get(chat_id, {}).get('from', '?')} → {user_data.get(chat_id, {}).get('to', '?')}",
    ]
    if num_passengers_needed:
        lines.append(f"👥 Для: {num_passengers_needed} пассажиров")
    lines.append("")
    lines.append("📌 Вагоны:")

    suitable_found = False
    for idx, carriage in enumerate(train['parsed_info']):
        seats_int = -1
        if carriage['seats'] != '?' and carriage['seats'].isdigit():
            seats_int = int(carriage['seats'])

        if num_passengers_needed and seats_int >= num_passengers_needed:
            lines.append(
                f"  ✅ {carriage['type']}: {carriage['seats']} мест × {carriage['price_byn']} BYN (хватает для {num_passengers_needed})"
            )
            suitable_found = True
        else:
            lines.append(
                f"  ❌ {carriage['type']}: {carriage['seats']} мест × {carriage['price_byn']} BYN"
            )

    if num_passengers_needed and not suitable_found:
        lines.append("\n⚠️ Нет вагонов с достаточным количеством мест.")

    full_text = "\n".join(lines)
    try:
        bot.send_message(chat_id, full_text)
    except Exception as e:
        logger.error(f"Ошибка при отправке детальной информации: {e}")

# --- ОБРАБОТЧИКИ КОМАНД И СООБЩЕНИЙ ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    logger.info(f"Пользователь {message.chat.id} ({message.from_user.username}) нажал /start")
    welcome_text = (
        "Привет! Я помогу тебе отслеживать билеты на поезда 🚂\n\n"
        "Команды:\n"
        "/track <откуда> <куда> <дата> <пассажиры> - начать отслеживание\n"
        "/stop - остановить отслеживание\n"
        "/status - проверить статус отслеживания\n\n"
        "Пример: /track Минск Москва 2026-04-10 2\n\n"
        "Я буду проверять наличие билетов каждые " + str(CHECK_INTERVAL) + " секунд и уведомлю тебя, когда места появятся!"
    )
    bot.reply_to(message, welcome_text)

@bot.message_handler(commands=['status'])
def check_status(message):
    chat_id = message.chat.id
    logger.info(f"Пользователь {chat_id} запросил статус")
    
    if chat_id in active_jobs:
        job_data = active_jobs[chat_id]['data']
        user_info = user_data.get(chat_id, {})
        status_text = (
            f"✅ Отслеживание активно\n\n"
            f"📍 Маршрут: {job_data['from']} → {job_data['to']}\n"
            f"📅 Дата: {job_data['date']}\n"
            f"🕐 Поезд: {job_data['selected_time']}\n"
            f"👥 Пассажиров: {user_info.get('passengers', 1)}\n\n"
            f"Интервал проверки: {CHECK_INTERVAL} сек."
        )
        bot.reply_to(message, status_text)
    else:
        bot.reply_to(message, "❌ У вас нет активных задач отслеживания.\nИспользуйте /track для начала.")

@bot.message_handler(commands=['track'])
def ask_for_route_and_date(message):
    chat_id = message.chat.id
    logger.info(f"Пользователь {chat_id} ({message.from_user.username}) отправил команду /track: {message.text}")
    
    if chat_id in active_jobs:
        bot.reply_to(message, "⚠️ У вас уже есть активное отслеживание. Используйте /stop чтобы остановить его, затем попробуйте снова.")
        return
    
    try:
        args = message.text.split()[1:]
        if len(args) != 4:
            raise ValueError("Неверное количество аргументов")

        from_station, to_station, date, num_passengers_str = args
        num_passengers = int(num_passengers_str)
        if num_passengers <= 0:
            raise ValueError("Количество пассажиров должно быть положительным числом")
        
        # Проверка формата даты
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            raise ValueError("Дата должна быть в формате YYYY-MM-DD")
            
    except ValueError as e:
        error_msg = (
            "❌ Неверный формат команды.\n\n"
            "Пример: `/track Минск Москва 2026-04-10 2`\n\n"
            "Где:\n"
            "- Минск - станция отправления\n"
            "- Москва - станция назначения\n"
            "- 2026-04-10 - дата (YYYY-MM-DD)\n"
            "- 2 - количество пассажиров"
        )
        logger.warning(f"Неверный формат команды от {chat_id}: {message.text}")
        bot.reply_to(message, error_msg, parse_mode="Markdown")
        return

    user_data[chat_id] = {'from': from_station, 'to': to_station, 'date': date, 'passengers': num_passengers}

    progress_msg = bot.reply_to(message, f"🔍 Получаю список поездов с {from_station} до {to_station} на {date} для {num_passengers} пассажиров...")
    logger.info(f"Запрашиваю поезда для {chat_id} - {from_station} -> {to_station}, {date}, {num_passengers} пасс.")

    trains = get_trains_list(from_station, to_station, date)

    if not trains:
        error_msg = "❌ Не удалось получить список поездов. Проверьте правильность названий станций и дату."
        logger.error(f"Не удалось получить поезда для {chat_id} - {from_station} -> {to_station}, {date}")
        bot.edit_message_text(chat_id=progress_msg.chat.id, message_id=progress_msg.message_id, text=error_msg)
        return

    keyboard = InlineKeyboardMarkup(row_width=1)
    for train in trains:
        time_str = train['time']
        num = train['num']
        duration = train['duration']
        info = train['parsed_info']
        num_passengers_needed = user_data[chat_id]['passengers']

        has_enough_seats = any(
            c['seats'] != '?' and c['seats'].isdigit() and int(c['seats']) >= num_passengers_needed
            for c in info
        )

        carriage_summary = ""
        if info:
            summary_items = []
            for c in info[:2]:
                summary_items.append(f"{c['type']}: {c['seats']}×{c['price_byn']}")
            if len(info) > 2:
                 summary_items.append("...")
            carriage_summary = "; ".join(summary_items)
        else:
            carriage_summary = "Нет данных"

        btn_text = f"{'✅' if has_enough_seats else '❌'} {time_str} | №{num} | {duration}"

        if len(btn_text) > 60:
            btn_text = btn_text[:57] + "..."

        button = InlineKeyboardButton(
            text=btn_text,
            callback_data=f"preview_{time_str}_{num}"
        )
        keyboard.add(button)

    bot.delete_message(chat_id=progress_msg.chat.id, message_id=progress_msg.message_id)
    bot.send_message(chat_id, f"Выберите поезд из списка (для {num_passengers} пассажиров):", reply_markup=keyboard)
    logger.info(f"Отправлен список поездов пользователю {chat_id}. Всего поездов: {len(trains)}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("preview_"))
def on_preview_selected(call):
    chat_id = call.message.chat.id
    logger.info(f"Пользователь {chat_id} выбрал поезд для просмотра: {call.data}")
    data = call.data.split("_", 2)
    if len(data) < 3:
        bot.answer_callback_query(call.id, "Ошибка данных.", show_alert=True)
        logger.error(f"Неверные данные в callback: {call.data}")
        return

    selected_time, selected_num = data[1], data[2]
    user_info = user_data.get(chat_id)

    if not user_info:
        bot.answer_callback_query(call.id, "Данные утеряны. Повторите /track.", show_alert=True)
        logger.warning(f"Данные пользователя {chat_id} не найдены в preview.")
        return

    trains = get_trains_list(user_info['from'], user_info['to'], user_info['date'])
    train = next((t for t in trains if t['time'] == selected_time and t['num'] == selected_num), None)

    if not train:
        bot.answer_callback_query(call.id, "Поезд не найден.", show_alert=True)
        logger.error(f"Поезд {selected_time} ({selected_num}) не найден для {chat_id} в preview.")
        return

    send_detailed_train_info(chat_id, train, user_info.get('passengers'))

    confirm_kb = InlineKeyboardMarkup()
    confirm_kb.add(
        InlineKeyboardButton("✅ Запустить отслеживание", callback_data=f"confirm_{selected_time}_{selected_num}"),
        InlineKeyboardButton("❌ Назад к списку", callback_data="back_to_list")
    )

    bot.send_message(chat_id, "Запустить отслеживание этого поезда?", reply_markup=confirm_kb)
    logger.info(f"Отправлены детали поезда {selected_time} и запрос на отслеживание пользователю {chat_id}.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_"))
def on_confirm_tracking(call):
    chat_id = call.message.chat.id
    logger.info(f"Пользователь {chat_id} подтвердил отслеживание: {call.data}")
    data = call.data.split("_", 2)
    if len(data) < 3:
        bot.answer_callback_query(call.id, "Ошибка данных.", show_alert=True)
        logger.error(f"Неверные данные в confirm callback: {call.data}")
        return

    selected_time, selected_num = data[1], data[2]

    if chat_id in active_jobs:
        bot.answer_callback_query(call.id, "Вы уже отслеживаете маршрут!", show_alert=True)
        logger.warning(f"Пользователь {chat_id} попытался запустить отслеживание, но оно уже активно.")
        return

    user_info = user_data.get(chat_id)
    if not user_info:
        bot.answer_callback_query(call.id, "Данные утеряны. Повторите /track.", show_alert=True)
        logger.warning(f"Данные пользователя {chat_id} не найдены при подтверждении отслеживания.")
        return

    bot.answer_callback_query(call.id, f"Отслеживание запущено для поезда в {selected_time}.")

    job_data = {
        'from': user_info['from'],
        'to': user_info['to'],
        'date': user_info['date'],
        'selected_time': selected_time,
        'selected_num': selected_num,
        'user_data': user_info
    }

    job_thread = threading.Thread(
        target=tracking_worker,
        args=(chat_id, user_info['from'], user_info['to'], user_info['date'], selected_time),
        daemon=True
    )
    job_thread.start()
    active_jobs[chat_id] = {'thread': job_thread, 'data': job_data}
    
    # Сохраняем задачу в файл
    save_active_jobs()
    
    logger.info(f"Запущен поток отслеживания для {chat_id}, поезд {selected_time}.")
    
    bot.send_message(
        chat_id, 
        f"✅ Отслеживание запущено!\n\n"
        f"Поезд: {selected_time}\n"
        f"Маршрут: {user_info['from']} → {user_info['to']}\n"
        f"Дата: {user_info['date']}\n\n"
        f"Я буду проверять наличие мест каждые {CHECK_INTERVAL} секунд.\n"
        f"Используйте /stop чтобы остановить отслеживание."
    )

@bot.callback_query_handler(func=lambda call: call.data == "back_to_list")
def on_back_to_list(call):
    chat_id = call.message.chat.id
    logger.info(f"Пользователь {chat_id} запросил возврат к списку поездов.")
    bot.answer_callback_query(call.id, "Возвращаемся к списку...")
    user_info = user_data.get(chat_id)
    if not user_info:
        bot.send_message(chat_id, "Данные утеряны. Повторите /track.")
        logger.warning(f"Данные пользователя {chat_id} не найдены при возврате к списку.")
        return

    from_station = user_info['from']
    to_station = user_info['to']
    date = user_info['date']
    num_passengers_needed = user_info.get('passengers', 1)

    trains = get_trains_list(from_station, to_station, date)

    if not trains:
        bot.send_message(chat_id, "❌ Не удалось получить список поездов.")
        logger.error(f"Не удалось получить поезда для {chat_id} при возврате к списку.")
        return

    keyboard = InlineKeyboardMarkup(row_width=1)
    for train in trains:
        time_str = train['time']
        num = train['num']
        duration = train['duration']
        info = train['parsed_info']

        has_enough_seats = any(
            c['seats'] != '?' and c['seats'].isdigit() and int(c['seats']) >= num_passengers_needed
            for c in info
        )

        carriage_summary = ""
        if info:
            summary_items = []
            for c in info[:2]:
                summary_items.append(f"{c['type']}: {c['seats']}×{c['price_byn']}")
            if len(info) > 2:
                 summary_items.append("...")
            carriage_summary = "; ".join(summary_items)
        else:
            carriage_summary = "Нет данных"

        btn_text = f"{'✅' if has_enough_seats else '❌'} {time_str} | №{num} | {duration}"

        if len(btn_text) > 60:
            btn_text = btn_text[:57] + "..."

        button = InlineKeyboardButton(
            text=btn_text,
            callback_data=f"preview_{time_str}_{num}"
        )
        keyboard.add(button)

    bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text=f"Выберите поезд из списка (для {num_passengers_needed} пассажиров):",
        reply_markup=keyboard
    )
    logger.debug(f"Отправлен обновлённый список поездов пользователю {chat_id}.")

@bot.message_handler(commands=['stop'])
def stop_tracking(message):
    chat_id = message.chat.id
    logger.info(f"Пользователь {chat_id} ({message.from_user.username}) запросил остановку отслеживания.")
    if chat_id in active_jobs:
        active_jobs.pop(chat_id)
        remove_job_from_file(chat_id)
        bot.reply_to(message, "✅ Отслеживание остановлено.")
        logger.info(f"Отслеживание для {chat_id} остановлено по команде пользователя.")
    else:
        bot.reply_to(message, "ℹ️ У вас нет активных задач.")
        logger.debug(f"Пользователь {chat_id} запросил stop, но отслеживание не активно.")

@bot.message_handler(func=lambda m: True)
def echo_all(message):
    chat_id = message.chat.id
    logger.info(f"Получено неизвестное сообщение от {chat_id} ({message.from_user.username}): {message.text}")
    bot.reply_to(message, "Я понимаю только команды: /start, /track, /stop, /status\n\nИспользуйте /start чтобы узнать как пользоваться ботом.")

def signal_handler(signum, frame):
    """Обработчик сигналов для корректного завершения"""
    logger.info(f"Получен сигнал {signum}. Сохранение состояния и завершение...")
    save_active_jobs()
    sys.exit(0)

if __name__ == '__main__':
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Загружаем активные задачи из файла
    load_active_jobs()
    
    logger.info("--- Бот запущен ---")
    try:
        bot.polling(none_stop=True, interval=1, timeout=10)
    except KeyboardInterrupt:
        logger.info("--- Бот остановлен пользователем ---")
        save_active_jobs()
    except Exception as e:
        logger.critical(f"Критическая ошибка в polling: {e}")
        save_active_jobs()
        raise e
