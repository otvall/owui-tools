# Инструменты для Open WebUI

- [Handoff Router](handoff_router/README.md) — комплект маршрутизации между
  оркестратором и сабагентами: один Pipe, два Preparation-фильтра и Tool
  `lite_delegate`. Исходники, генератор и тесты находятся рядом в `handoff_router/`.
- [Самостоятельные и прежние фильтры](optional_filters/README.md) — дополнительные
  Functions для обычных моделей и прежние адаптеры.
- [Локальная установка и демо](deployment/README.md) — Open WebUI, Rich UI
  и примеры SQL-графиков.

Для установки Router откройте инструкцию в `handoff_router/README.md`.
Настройка модели в интерфейсе и готовый системный промпт находятся в
[ROUTER_SETUP.md](handoff_router/ROUTER_SETUP.md).
Сам промпт: [ROUTER_SYSTEM_PROMPT.md](handoff_router/ROUTER_SYSTEM_PROMPT.md).
Четыре актуальных `.py`-файла для загрузки в Open WebUI лежат непосредственно
в `handoff_router/`.
