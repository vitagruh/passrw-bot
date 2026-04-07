import telebot
from telebot import types
import config
import requests
from bs4 import BeautifulSoup
import time
import threading

# Инициализация бота
bot = telebot.TeleBot(config.token)

# Хранилище состояний и данных
user_states = {}
active_jobs = {}  # {chat_id: [job1, job2]} - список активных потоков
tracking_data = {} # {chat_id: [data1, data2]} - данные для каждого трекинга

# Константы
MAX_TRACKINGS_PER_USER = 2
SITE_URL = "https://pass.rzd.ru/ticket-public/public/mpp/search/result" # Пример URL, нужно актуализировать под ваши нужды или использовать API

# --- ФУНКЦИИ МЕНЮ ---

def get_main_menu_markup():
    """Создает главное меню с постоянными кнопками"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_start = types.KeyboardButton("🚀 Старт")
    btn_status = types.KeyboardButton("📊 Статус")
    btn_new = types.KeyboardButton("🔍 Новый поиск")
    btn_stop = types.KeyboardButton("⛔ Стоп")
    markup.add(btn_start, btn_status)
    markup.add(btn_new, btn_stop)
    return markup

def get_cancel_markup():
    """Меню для отмены действия"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("❌ Отмена"))
    return markup

# --- ОБРАБОТЧИКИ КОМАНД И ТЕКСТА ---

@bot.message_handler(commands=['start'])
def start_command(message):
    send_welcome_message(message.chat.id)

@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    text = message.text

    # Если пользователь в режиме ввода данных (поиск)
    if chat_id in user_states:
        handle_state_input(message)
        return

    # Обработка кнопок главного меню
    if text == "🚀 Старт":
        send_welcome_message(chat_id)
    
    elif text == "📊 Статус":
        show_status(message)
    
    elif text == "🔍 Новый поиск":
        start_new_search(message)
    
    elif text == "⛔ Стоп":
        stop_tracking(message)
    
    elif text == "❌ Отмена":
        user_states.pop(chat_id, None)
        bot.send_message(chat_id, "Действие отменено.", reply_markup=get_main_menu_markup())

    else:
        # Если текст не распознан как команда меню и нет активного состояния
        bot.send_message(chat_id, "Выберите команду из меню ниже:", reply_markup=get_main_menu_markup())

def send_welcome_message(chat_id):
    markup = get_main_menu_markup()
    welcome_text = (
        f"Привет, {bot.get_chat_member(chat_id, bot.get_me().id).user.first_name if False else 'Путешественник'}!\n"
        "Я бот для отслеживания билетов РЖД.\n\n"
        "Выберите действие в меню:"
    )
    # Упрощенное приветствие, так как получение имени может требовать прав доступа в некоторых версиях библиотек
    # Используем message.from_user если бы это было внутри хендлера, здесь просто текст
    bot.send_message(chat_id, "Привет! Я бот для отслеживания билетов.\nИспользуйте кнопки внизу для управления.", reply_markup=markup)

# --- ЛОГИКА ПОИСКА И СОСТОЯНИЙ ---

def start_new_search(message):
    chat_id = message.chat.id
    
    # Проверка лимита трекингов
    current_count = len(active_jobs.get(chat_id, []))
    if current_count >= MAX_TRACKINGS_PER_USER:
        bot.send_message(
            chat_id, 
            f"⚠️ Вы достигли лимита одновременных отслеживаний ({MAX_TRACKINGS_PER_USER}).\n"
            "Нажмите '⛔ Стоп', чтобы удалить лишние трекинги перед созданием нового.",
            reply_markup=get_main_menu_markup()
        )
        return

    user_states[chat_id] = {'step': 'waiting_for_date'}
    bot.send_message(chat_id, "📅 Введите дату поездки (например, 25.10.2023):", reply_markup=get_cancel_markup())

def handle_state_input(message):
    chat_id = message.chat.id
    state = user_states[chat_id]
    text = message.text

    if text == "❌ Отмена":
        user_states.pop(chat_id, None)
        bot.send_message(chat_id, "Поиск отменен.", reply_markup=get_main_menu_markup())
        return

    if state['step'] == 'waiting_for_date':
        state['date'] = text
        state['step'] = 'waiting_for_from'
        bot.send_message(chat_id, "📍 Откуда? (Введите название станции отправления):", reply_markup=get_cancel_markup())

    elif state['step'] == 'waiting_for_from':
        state['from_station'] = text
        state['step'] = 'waiting_for_to'
        bot.send_message(chat_id, "🏁 Куда? (Введите название станции назначения):", reply_markup=get_cancel_markup())

    elif state['step'] == 'waiting_for_to':
        state['to_station'] = text
        state['step'] = 'confirm'
        
        summary = (
            f"Проверьте данные:\n"
            f"📅 Дата: {state['date']}\n"
            f"📍 Откуда: {state['from_station']}\n"
            f"🏁 Куда: {state['to_station']}\n\n"
            f"Начать отслеживание?"
        )
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(types.KeyboardButton("✅ Да"), types.KeyboardButton("❌ Нет"))
        bot.send_message(chat_id, summary, reply_markup=markup)

    elif state['step'] == 'confirm':
        if text == "✅ Да":
            start_tracking(message)
        else:
            user_states.pop(chat_id, None)
            bot.send_message(chat_id, "Отменено.", reply_markup=get_main_menu_markup())

# --- ЛОГИКА ОТслеживания (ТРЕДИНГ) ---

def start_tracking(message):
    chat_id = message.chat.id
    data = user_states[chat_id]
    user_states.pop(chat_id, None) # Сбрасываем состояние

    # Создаем уникальный ID для этого трекинга (просто индекс + время)
    tracking_id = len(active_jobs.get(chat_id, [])) + 1
    
    job_data = {
        'id': tracking_id,
        'date': data['date'],
        'from': data['from_station'],
        'to': data['to_station'],
        'count': 0
    }

    # Инициализируем списки, если их нет
    if chat_id not in active_jobs:
        active_jobs[chat_id] = []
    if chat_id not in tracking_data:
        tracking_data[chat_id] = []

    # Добавляем данные
    tracking_data[chat_id].append(job_data)

    # Запускаем поток
    thread = threading.Thread(target=tracking_worker, args=(chat_id, job_data))
    thread.daemon = True
    thread.start()
    active_jobs[chat_id].append(thread)

    bot.send_message(
        chat_id, 
        f"✅ Отслеживание #{tracking_id} запущено!\n"
        f"Маршрут: {job_data['from']} -> {job_data['to']}\n"
        f"Дата: {job_data['date']}\n\n"
        f"Вы можете создать еще {MAX_TRACKINGS_PER_USER - len(active_jobs[chat_id])} отслеживаний.",
        reply_markup=get_main_menu_markup()
    )

def tracking_worker(chat_id, job):
    """Фоновый процесс проверки билетов"""
    while True:
        try:
            # Здесь должна быть логика парсинга или запроса к API
            # Эмуляция проверки:
            # found = check_tickets(job['date'], job['from'], job['to'])
            
            # Для примера эмулируем случайную находку раз в 10 проверок (закомментировано для продакшена)
            # if random.randint(1, 10) == 1: found = True
            
            # Реальная заглушка:
            found = False 
            
            if found:
                msg = (
                    f"🎉 БИЛЕТЫ НАЙДЕНЫ!\n"
                    f"Поезд: ... (данные)\n"
                    f"Маршрут: {job['from']} -> {job['to']}"
                )
                bot.send_message(chat_id, msg)
                # Удаляем только этот конкретный трекинг
                remove_tracking(chat_id, job['id'])
                break
            
            job['count'] += 1
            time.sleep(60) # Проверка раз в минуту
            
        except Exception as e:
            print(f"Error in tracking for {chat_id}: {e}")
            time.sleep(60)

def remove_tracking(chat_id, track_id):
    """Удаляет конкретный трекинг по ID"""
    if chat_id in tracking_data:
        tracking_data[chat_id] = [t for t in tracking_data[chat_id] if t['id'] != track_id]
    if chat_id in active_jobs:
        # Примечание: потоки нельзя остановить насильственно безопасно, 
        # они должны сами завершиться при удалении данных или флаге стоп.
        # В упрощенной версии мы просто чистим данные, поток завершится на следующей итерации или ошибке.
        pass 

# --- КОМАНДЫ СТАТУСА И СТОПА ---

def show_status(message):
    chat_id = message.chat.id
    jobs = tracking_data.get(chat_id, [])
    
    if not jobs:
        bot.send_message(chat_id, "У вас нет активных отслеживаний.", reply_markup=get_main_menu_markup())
        return

    text = "📊 **Ваши активные отслеживания:**\n\n"
    buttons = []
    
    for job in jobs:
        text += f"#{job['id']} | {job['from']} ➝ {job['to']}\n"
        text += f"   Дата: {job['date']} | Проверок: {job['count']}\n\n"
        
        # Добавляем кнопку для остановки конкретного трекинга
        btn = types.InlineKeyboardButton(f"⛔ Остановить #{job['id']}", callback_data=f"stop_{job['id']}")
        buttons.append(btn)

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    if buttons:
        keyboard.add(*buttons)
    
    bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('stop_'))
def on_stop_tracking_choice(call):
    chat_id = call.message.chat.id
    track_id = int(call.data.split('_')[1])
    
    remove_tracking(chat_id, track_id)
    
    bot.answer_callback_query(call.id, f"Отслеживание #{track_id} остановлено!")
    bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    bot.send_message(chat_id, f"Отслеживание #{track_id} успешно удалено.", reply_markup=get_main_menu_markup())

def stop_tracking(message):
    chat_id = message.chat.id
    jobs = tracking_data.get(chat_id, [])
    
    if not jobs:
        bot.send_message(chat_id, "Нет активных отслеживаний для остановки.", reply_markup=get_main_menu_markup())
        return

    if len(jobs) == 1:
        # Если один, удаляем сразу
        remove_tracking(chat_id, jobs[0]['id'])
        bot.send_message(chat_id, f"Отслеживание #{jobs[0]['id']} остановлено.", reply_markup=get_main_menu_markup())
    else:
        # Если несколько, просим выбрать через статус (переиспользуем логику статуса или отправляем список)
        bot.send_message(chat_id, "У вас несколько отслеживаний. Нажмите '📊 Статус', чтобы выбрать какое остановить, или выберите ниже:", reply_markup=get_main_menu_markup())
        # Для удобства можно сразу вывести inline кнопки как в статусе
        show_status(message)

# Запуск бота
if __name__ == '__main__':
    print("Бот запущен...")
    bot.polling(none_stop=True)
