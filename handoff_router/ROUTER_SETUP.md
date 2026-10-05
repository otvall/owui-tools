# Настройка Router-модели и системный промпт

Инструкция для актуального комплекта Handoff Router 0.21.1, рассчитанного на
Open WebUI 0.11.1. Пользователь выбирает одну Workspace Model `Router`, а Pipe
вызывает модель-оркестратор или передаёт текущий запрос одному сабагенту.

## 1. Установите компоненты

В **Admin Panel → Functions → Create** создайте три Functions, вставляя целиком
содержимое соответствующих файлов. Включите их после сохранения.
Тип определяется по коду автоматически. Это стандартная установка
[Functions в Open WebUI](https://docs.openwebui.com/features/extensibility/plugin/functions/).

| Файл | Пример ID Function | Тип |
|---|---|---|
| [lite_handoff_router.py](lite_handoff_router.py) | `lite_handoff_router` | Pipe |
| [router_preparation.py](router_preparation.py) | `router_preparation` | Filter |
| [subagent_preparation.py](subagent_preparation.py) | `subagent_preparation` | Filter |

В **Workspace → Tools** создайте Tool с ID `lite_delegate` и вставьте код
из [lite_delegate.py](lite_delegate.py). Tools устанавливаются в отдельном
[разделе Workspace](https://docs.openwebui.com/features/extensibility/plugin/tools/).
Если Tool уже установлен под другим ID, укажите этот ID в `base_tool_ids` ниже;
имя вызываемого метода остаётся `lite_delegate`.

У обоих Preparation-фильтров оставьте **Global выключенным**.
Подкаталоги `shared/`, `tools/`, `tests/` в Open WebUI загружать не требуется.

## 2. Укажите модель-оркестратор в Valves Pipe

Откройте **Admin Panel → Functions → Lite Handoff Router → Valves**.

| Valve | Значение |
|---|---|
| `orchestrator_model_id` | Точный ID подключённой модели, поддерживающей native Tool calls |
| `emit_handoff_status` | `true` — показывать статус передачи сабагенту |
| `debug` | `false` для обычной работы |

`orchestrator_model_id` — модель, которая принимает решение о маршрутизации.
Это отдельный ID реальной модели, а не ID Pipe `lite_handoff_router` и не ID
публичной Workspace Model `Router`.

## 3. Создайте публичную Workspace Model Router

Откройте **Workspace → Models → Create**. Workspace Model хранит базовую модель,
системный промпт, параметры и attachments; это стандартный
[редактор Models](https://docs.openwebui.com/features/workspace/models/).

| Поле | Что указать |
|---|---|
| Name | `Router` |
| Model ID | Например, `handoff-router`; сохраните фактический ID |
| Base Model | Установленный Pipe **Lite Handoff Router** |
| System Prompt | Текст из блока «Готовый системный промпт» ниже |
| Filters | Только **Router Preparation** из компонентов этого комплекта |
| Tools | Выберите **Lite Delegate** |
| Function Calling | **Native** |
| Builtin Tools | Включите для загрузки Skills через `view_skill` |
| Access | Дайте доступ пользователям, которые будут общаться с Router |

Режим Native требует поддержки вызова Tools моделью-оркестратором;
[документация Open WebUI по Tool calling](https://docs.openwebui.com/features/extensibility/plugin/tools/)
описывает его настройку.

Промпт вставляется именно в **System Prompt этой Workspace Model**.
Сохраните карточку и в чате выбирайте созданный `Router`.
Пользователям этой модели также нужен доступ к Tool `lite_delegate` и Skills,
которые они должны использовать.

## 4. Создайте и зарегистрируйте сабагентов

Для каждого сабагента создайте отдельную **Workspace Model**:

- выберите его реальную Base Model;
- задайте его собственный системный промпт и рабочие Tools, Skills, MCP, Knowledge;
- прикрепите **Subagent Preparation**;
- настройте доступ для пользователей Router.

В [Workspace → Skills](https://docs.openwebui.com/features/workspace/skills/)
создайте активный routing Skill для каждого сабагента.
Его описание объясняет назначение агента, а полное содержание задаёт условия
передачи и точный `agent_id`. Например:

| Поле Skill | Пример |
|---|---|
| Name | `Маршрутизация вычислений` |
| Description | `Выбирай этого агента для вычислений, формул и проверки числовых результатов.` |
| ID | В примере ниже `route-math-agent`; используйте фактический ID сохранённого Skill |

Содержимое этого routing Skill:

```text
Агент math-agent выполняет вычисления и проверяет числовые результаты.
Передавай ему запросы на расчёты и применение формул.
Для передачи вызови lite_delegate с agent_id="math-agent".
Этот ID обозначает Workspace Model сабагента.
```

Затем откройте код установленного **Router Preparation** и замените демонстрационный
словарь `SUBAGENTS` своим каталогом:

```python
SUBAGENTS: dict[str, str] = {
    "math-agent": "route-math-agent",
}
```

Здесь оба ID приведены для примера: замените их реальными IDs модели и Skill,
а ID модели также исправьте в тексте routing Skill. Слева всегда **Workspace
Model ID сабагента**, справа — **Routing Skill ID**. Добавляйте других агентов
отдельными записями словаря. Сохраните Function после изменения каталога.

Routing Skills Router получает из `SUBAGENTS` автоматически. Рабочие Skills
сабагента выбираются отдельно в его карточке модели.

## 5. Настройте Preparation-фильтры

Для минимального запуска задайте Valves **Router Preparation**:

В интерфейсе Valves поля списков принимают значения **через запятую**, а не JSON.
Для `base_tool_ids` введите `lite_delegate` без скобок и кавычек.
Для пустого `base_skill_ids` переключите поле в **Custom** и введите
**одну запятую `,` без кавычек**, затем нажмите Save. Полностью пустое поле
не проходит обязательную проверку формы, а `[]` сохранится как ID Skill.
При сохранении запятая преобразуется в пустой список: редактор разделяет строку
по запятым и удаляет пустые элементы. Это поведение проверено в
[исходнике редактора Valves Open WebUI 0.11.1](https://github.com/open-webui/open-webui/blob/v0.11.1/src/lib/components/workspace/common/ValvesModal.svelte).

Ниже — итоговые значения в формате JSON, а не текст для отдельных полей формы:

```json
{
  "priority": -100,
  "base_tool_ids": ["lite_delegate"],
  "base_skill_ids": [],
  "enabled": true,
  "debug": false
}
```

Пустой `base_skill_ids` отключает только дополнительные базовые Skills
оркестратора: routing Skills из `SUBAGENTS` продолжат загружаться.
Для этой настройки достаточно системного промпта ниже и ваших routing Skills.
Если используете базовые Skills, добавьте их реальные IDs в этот список.
Стандартные IDs `orchestrator-capability-guide` и `describe-available-agents`
не создаются автоматически; оставлять их в Valves можно только при наличии
соответствующих активных Skills.

У **Subagent Preparation** оставьте начальные значения:

```json
{
  "priority": -30,
  "history_turns": 0,
  "history_tool_calls": 0,
  "debug": false
}
```

При `history_turns = 0` сабагент получает текущий запрос без предыдущих
диалоговых туров. Увеличьте этот лимит, если ему нужна предыдущая переписка;
`history_tool_calls` отдельно ограничивает прошлые вызовы Tools в этих турах.

Если обновляете прежнюю установку, снимите с Router фильтры **Lite Subagent
Registry**, **Previous Tool Context**, **History Cleanup**, а с сабагентов —
**Tool Call Filter**, **Subagent Context**, **Skill Context**. Снимите также
их Global attachments. Таблица attachments актуального комплекта:

| Workspace Model | Preparation-фильтр |
|---|---|
| Router | Router Preparation |
| Каждый сабагент | Subagent Preparation |

## Готовый системный промпт

Скопируйте целиком содержимое [ROUTER_SYSTEM_PROMPT.md](ROUTER_SYSTEM_PROMPT.md)
в System Prompt Workspace Model `Router`. В этом файле находятся только
инструкции для модели. Список агентов в промпте не требуется: его описывают
актуальные routing Skills.

## Проверка после настройки

Начните новый чат с `Router` и отправьте задачу, явно соответствующую созданному
routing Skill. Для примера выше: «Рассчитай 17% от 2450 и покажи вычисление».
При выборе агента появится статус `delegate to …`, если `emit_handoff_status`
включён. Новый запрос снова начинает выбор через оркестратор.

| Симптом | Что проверить |
|---|---|
| `orchestrator_model_id is not configured` | Заполните Valves именно Pipe Lite Handoff Router |
| `Configured model-bound Skills are unavailable` | В поле `base_skill_ids` введите одну запятую `,` для пустого списка либо IDs существующих активных базовых Skills через запятую |
| `Router Model is unavailable` | Используйте сохранённую активную Workspace Model Router на основе Pipe |
| Ошибка требует Router Preparation | Проверьте активность фильтра и attachment на Router; после исправления начните новый запрос |
| Агент не выбирается | Проверьте IDs в `SUBAGENTS`, активность routing Skill, описание специализации и доступ пользователя к модели |
| Модель пишет о передаче текстом | Проверьте Native Tool calling, доступ к Tool и поддержку Tool calls моделью-оркестратором |

Подробности поведения и обновления комплекта находятся в [README](README.md).
