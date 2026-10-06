# Самостоятельные и прежние фильтры Open WebUI

В этой папке находятся дополнительные Functions для обычных моделей и прежний
адаптер Registry. Актуальный [Handoff Router](../handoff_router/README.md)
содержит один Pipe, Router Preparation и Tool `lite_delegate`.

Все `.py`-файлы загружаются в Open WebUI независимо, без соседних Python-модулей.

## Фильтры для обычных моделей

| Файл | Назначение |
|---|---|
| [previous_tool_context.py](previous_tool_context.py) | Строковая запись завершённых Tool Calls предыдущего запроса |
| [history_cleanup.py](history_cleanup.py) | Очистка прошлых нативных Tool Calls |
| [tool_call_filter.py](tool_call_filter.py) | Проекция истории на доступные модели Tools |
| [subagent_context.py](subagent_context.py) | Лимиты прошлых текстовых ходов и Tool exchanges |
| [skill_context.py](skill_context.py) | Skill context и builtin `view_skill` для прикреплённых Skills |
| [tool_call_tombstone_context.py](tool_call_tombstone_context.py) | Обрезка истории с сохранением минимальных пар использованных Tool Call ID |

Skill Context можно прикреплять к обычным моделям без Router Pipe.
Tool Call Filter, Subagent Context и Skill Context сохраняют свои самостоятельные
Valves. В актуальном Router подготовка сабагентов выполняется внутри Pipe.
Skill Context использует штатный `view_skill` Open WebUI: доступность загрузки
Skill определяется его активностью и правами пользователя, без дополнительной
проверки принадлежности текущему manifest.

## Прежний адаптер Registry

[lite_subagent_registry.py](lite_subagent_registry.py) сохранён как прежняя
отдельная Function. В актуальной конфигурации реестр готовит Router Preparation.

## Самостоятельное использование контекстных фильтров

`previous_tool_context.py` и `history_cleanup.py` можно прикрепить к
любой Workspace Model. Registry, Orchestrator Skills и Router Pipe для этого не
нужны. Если нужны одновременно строковая запись и очистка нативной истории,
прикрепите оба фильтра с priorities `-90` и `-80` соответственно.

Previous Tool Context добавит перед текущим запросом обычное сообщение
`assistant` с полными завершёнными парами `Tool Call` / `Tool Result`
непосредственно предыдущего запроса. Без Registry исполнитель всех Tool exchanges
отмечается как `model`, включая историю с Handoff-маркерами. С Registry фильтр
определяет Handoff и подписывает сабагента.

History Cleanup удалит нативные `tool_calls`, сообщения `tool` и служебные
сообщения с изображениями Tools из прошлых запросов. Обычные вопросы, ответы и
добавленная строковая запись останутся. Сообщения от последнего пользовательского
запроса и дальше сохраняются без изменений, поэтому уже начатая текущая
Tool-цепочка не повреждается.

### Tool Call Tombstone Context для обычных чатов

`tool_call_tombstone_context.py` — альтернативный самостоятельный фильтр для
обычных Workspace/Base Models. По умолчанию он оставляет последние пять
завершённых текстовых ходов и текущий запрос. Прошлые нативные вызовы и их
результаты заменяются одним непрерывным tombstone-блоком сразу после системных
сообщений: один `assistant.tool_calls` содержит использованные ID и имена
функций с пустыми аргументами `{}`, а каждому ID соответствует минимальный
`tool`-ответ `[omitted]`.

Фильтр сохраняет только завершённые пары, удаляет исходные аргументы, результаты
и служебные поля Open WebUI, дедуплицирует повторные ID и не создаёт tombstone
для ID, уже присутствующего в текущей Tool-цепочке. Число сохранённых текстовых
ходов задаётся Valve `history_turns` (по умолчанию `5`).

Не подключайте Tool Call Tombstone Context одновременно с History Cleanup или
Subagent Context: они реализуют взаимоисключающие политики истории. В Router
подготовку истории выполняет Pipe; Tombstone Context предназначен для обычных
чатов. Фильтр
предоставляет модели старые ID как контекст, но продолжение числового счётчика
остаётся поведением конкретной модели или backend. Если ID всё равно повторяются,
их нужно переназначать на UUID до исполнения и сохранения Tool Call.

Как и другие inlet-фильтры Open WebUI v0.11.1, он не запускается повторно при
внутренних продолжениях Tool Calls.

## Разработка и проверка

Общие авторитетные исходники находятся в `handoff_router/shared/`.
Генератор обновляет оба каталога. Tool Call Tombstone Context редактируется
непосредственно и не содержит генерируемых блоков.

Команды выполняются из корня репозитория:

```sh
python3 handoff_router/tools/generate_skill_preparation.py --check
python3 -B -m unittest discover -s handoff_router/tests -v
```
