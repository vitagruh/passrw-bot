import asyncio
import time
from typing import Dict, Set
from logging_config import logger

class TrainTracker:
    """Класс для отслеживания поездов с интервалом 60 секунд."""
    
    def __init__(self, check_interval: int = 60):
        self.check_interval = check_interval  # Интервал проверки в секундах
        self._tracked_trains: Set[str] = set()  # Множество отслеживаемых поездов
        self._running: bool = False
        self._task: asyncio.Task | None = None
        logger.info(f"TrainTracker инициализирован с интервалом {check_interval} сек")

    def add_train(self, train_number: str) -> bool:
        """
        Добавить поезд в список отслеживаемых.
        
        Args:
            train_number: Номер поезда
            
        Returns:
            True если поезд добавлен, False если уже отслеживается
        """
        if train_number in self._tracked_trains:
            logger.warning(f"Поезд {train_number} уже отслеживается")
            return False
        
        self._tracked_trains.add(train_number)
        logger.info(f"Добавлен поезд для отслеживания: {train_number}. Всего отслеживается: {len(self._tracked_trains)}")
        return True

    def remove_train(self, train_number: str) -> bool:
        """
        Удалить поезд из списка отслеживаемых.
        
        Args:
            train_number: Номер поезда
            
        Returns:
            True если поезд удален, False если не найден
        """
        if train_number not in self._tracked_trains:
            logger.warning(f"Поезд {train_number} не найден в списке отслеживаемых")
            return False
        
        self._tracked_trains.remove(train_number)
        logger.info(f"Удален поезд из отслеживания: {train_number}. Осталось: {len(self._tracked_trains)}")
        return True

    def get_tracked_trains(self) -> list:
        """
        Получить список всех отслеживаемых поездов.
        
        Returns:
            Список номеров поездов
        """
        trains_list = sorted(list(self._tracked_trains))
        logger.debug(f"Запрошен список отслеживаемых поездов: {len(trains_list)} шт.")
        return trains_list

    def stop_tracking(self, train_number: str | None = None) -> None:
        """
        Остановить отслеживание конкретного поезда или всех поездов.
        
        Args:
            train_number: Номер поезда для остановки. Если None - останавливаются все.
        """
        if train_number is None:
            count = len(self._tracked_trains)
            self._tracked_trains.clear()
            logger.info(f"Остановлено отслеживание всех поездов. Удалено: {count}")
        else:
            self.remove_train(train_number)

    async def _check_trains_loop(self) -> None:
        """Внутренний цикл проверки поездов."""
        logger.info("Запущен цикл проверки поездов")
        
        while self._running:
            try:
                logger.info(f"Начало проверки {len(self._tracked_trains)} поездов")
                
                if self._tracked_trains:
                    for train in self._tracked_trains:
                        # Здесь будет логика проверки статуса поезда
                        # Пока просто логируем процесс
                        logger.info(f"Проверка поезда: {train}")
                        await self._check_single_train(train)
                else:
                    logger.debug("Список отслеживаемых поездов пуст")
                
                logger.info(f"Проверка завершена. Следующая через {self.check_interval} сек")
                await asyncio.sleep(self.check_interval)
                
            except asyncio.CancelledError:
                logger.info("Цикл проверки остановлен")
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле проверки: {e}", exc_info=True)
                await asyncio.sleep(5)  # Пауза перед повторной попыткой

    async def _check_single_train(self, train_number: str) -> None:
        """
        Проверка статуса одного поезда.
        Здесь должна быть реальная логика получения данных.
        """
        # Заглушка для реальной логики
        logger.debug(f"Выполняется проверка данных для поезда {train_number}")
        await asyncio.sleep(0.1)  # Имитация запроса

    async def start(self) -> None:
        """Запустить отслеживание поездов."""
        if self._running:
            logger.warning("Отслеживание уже запущено")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._check_trains_loop())
        logger.info("Отслеживание поездов запущено")

    async def stop(self) -> None:
        """Остановить отслеживание поездов."""
        if not self._running:
            logger.warning("Отслеживание не запущено")
            return
        
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        
        logger.info("Отслеживание поездов остановлено")

    def is_running(self) -> bool:
        """Проверить, запущено ли отслеживание."""
        return self._running


# Пример использования
async def main():
    tracker = TrainTracker(check_interval=60)
    
    # Добавляем поезда
    tracker.add_train("SAPSAN-752A")
    tracker.add_train("NEVSKY-EXPRESS-740A")
    tracker.add_train("LASTOCHKA-802M")
    
    # Получаем список
    trains = tracker.get_tracked_trains()
    logger.info(f"Список поездов: {trains}")
    
    # Запускаем отслеживание
    await tracker.start()
    
    # Работаем некоторое время
    try:
        await asyncio.sleep(180)  # 3 минуты работы
        
        # Удаляем один поезд
        tracker.stop_tracking("SAPSAN-752A")
        
        # Продолжаем работу
        await asyncio.sleep(120)  # Еще 2 минуты
        
    except KeyboardInterrupt:
        logger.info("Получен сигнал остановки")
    finally:
        # Останавливаем всё
        tracker.stop_tracking()  # Очищаем список
        await tracker.stop()     # Останавливаем цикл
        logger.info("Работа завершена")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Программа прервана пользователем")
