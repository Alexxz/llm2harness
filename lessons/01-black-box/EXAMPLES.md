# Происхождение примеров и актуализация

Общий порядок: [EXAMPLE_PROVENANCE.md](../../course/EXAMPLE_PROVENANCE.md). Исходные промпты, fixtures, критерии и настройки находятся в этом каталоге; lesson.json задаёт маршрут. Агент, который перерабатывает урок, сначала читает INSTRUCTOR и EXPLANATIONS (если есть), затем сравнивает их с программой и фактическим журналом. Внутри занятия эти инструкции не превращают внешнего помощника в преподавателя.

Сохранённые авторские опыты:

- [lesson-01-example-candidates.json](../../evidence/lesson-01-example-candidates.json)
- [lesson-01-live-pilot-v2.json](../../evidence/lesson-01-live-pilot-v2.json)
- [lesson-01-live-pilot.json](../../evidence/lesson-01-live-pilot.json)
- [lesson-01-preflight-v2.json](../../evidence/lesson-01-preflight-v2.json)
- [lesson-01-preflight.json](../../evidence/lesson-01-preflight.json)
- [lesson-01-repair-preflight-20261117.json](../../evidence/lesson-01-repair-preflight-20261117.json)
- [lesson-01-simple-example-pilot.json](../../evidence/lesson-01-simple-example-pilot.json)

Исторический профиль публичной модели указан в этих отчётах и resources/models.json. При повторении сохраняйте новый отчёт; старые ответы не заменяйте сгенерированными заново.

Повтор: проверка среды → исходный запрос → изменение одного фактора → второй запрос → независимая проверка критерия. Сохраняйте точный вход, полный ответ, версии весов/runtime, параметры, ошибки и вывод; для обучения также данные, `seed` и хеш адаптера. Проверяйте новый случай, чтобы не подгонять объяснение под один ответ. Если механизм не наблюдается, опишите ограничение и измените пример отдельной редакцией. Никакое описание здесь не запускает занятие автоматически.
