# Инструменты для Open WebUI

- [Handoff Router](handoff_router/README.md) — комплект маршрутизации между
  оркестратором и сабагентами: один Pipe, Router Preparation и Tool
  `lite_delegate`. Исходники, генератор и тесты находятся рядом в `handoff_router/`.
- [Самостоятельные и прежние фильтры](optional_filters/README.md) — дополнительные
  Functions для обычных моделей и прежние адаптеры.
- [Saved HTML Widgets](saved_html_widgets/README.md) — поиск и чтение исходного
  HTML графиков, таблиц и других виджетов из предыдущих ответов текущей ветки.
- [Локальная установка и демо](deployment/README.md) — Open WebUI, Rich UI
  и примеры SQL-графиков.

Для установки Router откройте инструкцию в `handoff_router/README.md`.
Настройка модели в интерфейсе и готовый системный промпт находятся в
[ROUTER_SETUP.md](handoff_router/ROUTER_SETUP.md).
Сам промпт: [ROUTER_SYSTEM_PROMPT.md](handoff_router/ROUTER_SYSTEM_PROMPT.md).
Три актуальных `.py`-файла для загрузки в Open WebUI лежат непосредственно
в `handoff_router/`.
