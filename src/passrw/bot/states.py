from aiogram.fsm.state import State, StatesGroup


class SearchFlow(StatesGroup):
    from_station = State()
    to_station = State()
    travel_date = State()
    passengers = State()
    results = State()


class SettingsFlow(StatesGroup):
    quiet = State()
    max_price = State()
