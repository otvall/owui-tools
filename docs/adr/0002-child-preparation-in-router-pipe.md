---
status: accepted
---

# Подготовка сабагента внутри Router Pipe

При каждом Handoff и продолжении запроса Pipe автоматически готовит выбранную
Workspace Model: свежие capabilities и доступность Skill loader, проекция Tool
history, общие лимиты истории, свежий Skill context. Это снимает обязательный
Subagent Preparation attachment с каждой модели и сокращает установку до Router
Pipe, Router Preparation и `lite_delegate`. Решение согласовано в
[спецификации #17](https://github.com/otvall/owui-tools/issues/17); автоматическая
подготовка реализуется в [#18](https://github.com/otvall/owui-tools/issues/18).

Подготовка всегда первая. Затем Open WebUI выполняет дополнительные inlet-фильтры
одним штатным dispatch, сохраняя их относительный порядок и окончательную очистку
файлов. Их изменения попадают в запрос и Tool context без повторной подготовки
или новой финальной проверки `view_skill`. В обмен на простой onboarding убрана
возможность размещать подготовку между дополнительными фильтрами. `history_turns`,
`history_tool_calls` и диагностика сабагента находятся в общих Valves Pipe;
Router Preparation сохраняет свою конфигурацию и ответственность.

Это решение заменяет ADR 0001 в части child attachment и preparation priority.
Миграция ручная: перенести лимиты в Pipe, выбрать его `debug` и снять прежние
child-фильтры, включая global attachments. Специальной обработки оставшихся
attachments нет. Обычные самостоятельные фильтры сохраняются; предыдущая
архитектура доступна под тегом `subagent-preparation-filter`. Изменение политики
native Skill loading из #17 выполняется отдельной задачей.
