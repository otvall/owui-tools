# Lite Handoff Router для Open WebUI v0.11.1

Версия комплекта: **0.21.1**.

Актуальный комплект состоит из **одного Pipe, двух фильтров и одного Tool**.
Все четыре готовых Function-файла находятся в корне `handoff_router/`.
Подкаталоги нужны для разработки:

- `shared/` — общие исходники, встроенные генератором в готовые Functions.
- `tools/` — генератор.
- `tests/` — регрессии и fixtures.

Имена файлов в инструкции ниже указаны относительно `handoff_router/`.

Pipe отвечает за runtime-маршрутизацию и capabilities выбранной модели. Перед
каждым вызовом сабагента он вручную запускает inlet pipeline Workspace Model
адресата с одним фильтром Subagent Preparation.

## Компоненты

| Файл | Тип Function | Назначение |
|---|---|---|
| [lite_handoff_router.py](lite_handoff_router.py) | Pipe | Выбирает модель, запускает её фильтры и ведёт текущий Handoff |
| [router_preparation.py](router_preparation.py) | Filter | Готовит реестр, исходную Tool history, справочную запись и очистку Router |
| [subagent_preparation.py](subagent_preparation.py) | Filter | Подготавливает Tool history, общие лимиты истории и Skill context всех сабагентов |
| [lite_delegate.py](lite_delegate.py) | Tool | Возвращает маркер выбора сабагента |

Все файлы самостоятельны: при установке в Open WebUI они не импортируют друг
друга как Python-модули. Фильтры обмениваются только request-scoped значениями
в `metadata`.

Для первой установки используйте [настройку Router-модели и готовый системный
промпт](ROUTER_SETUP.md). Текст для вставки в System Prompt вынесен в
[ROUTER_SYSTEM_PROMPT.md](ROUTER_SYSTEM_PROMPT.md).

## Установка обновления

1. Загрузите Router Preparation из `router_preparation.py`, Subagent Preparation
   из `subagent_preparation.py` и Pipe из `lite_handoff_router.py`.
   Установите Tool из `lite_delegate.py` или сохраните уже установленный
   `lite_delegate`.
2. Прикрепите к публичной Router Workspace Model **Router Preparation** вместо
   **Lite Subagent Registry, Previous Tool Context и History Cleanup**. Снимите
   все три прежних attachment-а на Router, включая global attachments.
3. Перенесите настройки и каталог из прежних Functions в Router Preparation:

   | Источник | Настройка Router Preparation |
   |---|---|
   | Registry `base_tool_ids` | `base_tool_ids` (по умолчанию `["lite_delegate"]`) |
   | Registry `base_skill_ids` | `base_skill_ids` (по умолчанию `orchestrator-capability-guide`, `describe-available-agents`) |
   | Previous Tool Context `enabled` | `enabled` (по умолчанию `True`) |
   | Каталог `SUBAGENTS` в коде Registry | `SUBAGENTS` в коде Router Preparation |

   `SUBAGENTS` по-прежнему сопоставляет Workspace Model ID и Routing Skill ID.
   Зарегистрируйте агентов в этом каталоге, создайте активные routing Skills и
   выберите Tools, Skills, MCP, Knowledge и inference settings на каждой
   Workspace Model. Сам attachment Router Preparation агентов не регистрирует.
4. Прикрепите один экземпляр **Subagent Preparation** к каждой Workspace Model
   сабагента вместо **Tool Call Filter, Subagent Context и Skill Context**.
   Снимите три прежних attachment-а у адресатов и их global attachments.
   Настройки `history_turns` и `history_tool_calls` из Subagent Context перенесите
   в общие Valves Subagent Preparation; по умолчанию оба значения равны `0`.
   Все модели с одним экземпляром Function получают одинаковые лимиты.
5. Оставьте значения `priority`, указанные по умолчанию:

   | Filter | Priority |
   |---|---:|
   | Router Preparation | `-100` |
   | Subagent Preparation | `-30` |

6. В Valves Lite Handoff Router укажите `orchestrator_model_id`, как и раньше.

Router Preparation нужно прикрепить именно к Router Workspace Model.
Subagent Preparation должен быть прикреплён к каждой модели адресата: внутренний вызов из
Pipe не запускает обычный inlet pipeline, поэтому Router получает их через
`get_filter_functions()` и последовательно вызывает `process_filter_functions()`.
Не отмечайте эти два фильтра как global: иначе Router- и subagent-цепочки
смешаются до того, как Pipe выберет модель адресата.

Обязательная Router inlet-цепочка: **Router Preparation → Pipe**. Внутри Function
фиксирован порядок **Registry → Previous Tool Context → History Cleanup**.
Отдельных priorities внутренних стадий нет; `priority` определяет положение
всей Function относительно дополнительных фильтров, а `debug` включает её общий
лог. Pipe проверяет полное свидетельство подготовки текущего запроса перед
отправкой оркестратору или сабагенту. Ошибка неполной либо устаревшей подготовки
предлагает прикрепить Router Preparation; начните новый запрос после исправления.

Оба фильтра доступны в этой версии. Обязательный набор содержит Router Preparation,
Subagent Preparation, Pipe и delegate Tool. Предварительная проверка смешанных
новых и прежних attachments и полный migration deliverable остаются отдельной
задачей: здесь замените соответствующие attachments вручную по указанным ролям.

Самостоятельные фильтры для обычных моделей и прежние адаптеры находятся
в [отдельном каталоге `optional_filters/`](../optional_filters/README.md).

## Поток запроса

При новом сообщении пользователя:

1. Стадия Registry внутри Router Preparation проверяет доступ
   к агентам и сохраняет реестр, базовые Tools и routing Skills.
   После проверки конфигурации она очищает request-scoped состояние прошлого запуска.
2. Стадия Previous Tool Context читает исходную историю. Для оркестратора она добавляет
   обычное сообщение `assistant` с JSON всех завершённых пар «вызов — результат»
   непосредственно предыдущего запроса. Вопрос и итоговый ответ в этот блок не
   копируются.
3. Стадия History Cleanup оставляет штатную текстовую переписку, удаляет прошлые
   нативные `tool_calls` и сообщения `tool`, но сохраняет текущий запрос и его
   незавершённую цепочку без изменений.
4. Router заново читает прикреплённые routing Skills и строит их Skill context.
   При доступном builtin `view_skill`, session и non-legacy calling mode это
   manifest; иначе — полное содержимое Skills. Также учитывается builtin Tools
   capability модели.
5. Router подключает реальные callables оркестратора и вызывает
   `orchestrator_model_id`.

Продолжения Tool Calls внутри того же запроса используют сохранённое
свидетельство inlet-цепочки. Router Preparation в provider continuation loop заново
не запускается. На новом сообщении пользователя её стадия Registry сбрасывает прежнее
свидетельство, поэтому пропущенный фильтр не может использовать успех прошлого
запроса.

После `lite_delegate` pipe сам переключает модель и набор capabilities. Текущие
вызовы и результаты сабагента остаются в нативном формате `tool_calls` / `tool`.
На каждом продолжении Pipe снова запускает фильтры модели адресата, поэтому
текущая Tool-цепочка проверяется тем же способом.

Подготовку дочернего запроса целиком ведёт `ChildRequestBuilder` внутри файла
Router: он получает типизированные данные маршрутизации, вызывает подготовку
Workspace Model и запускает фильтры адресата.
Pipe получает готовый запрос и выбранного агента, отправляет status и вызывает
completion adapter.

Module `WorkspaceModelPreparation` в Router предоставляет один interface:
`prepare(model_id, prepared=..., runtime=..., context=...)`. Он проверяет
доступность выбранной модели, читает свежую запись Workspace Model, нормализует
прикреплённые Tools/Skills, очищает внешние inference fields и подготавливает
capabilities через существующий кеш `RequestRuntime`. Результат
`PreparedWorkspaceModel` содержит `runtime_model` и `CapabilitySet`; списки
Tool/Skill IDs берутся из этого же `CapabilitySet`.

При совпадении настроенного Model ID и нормализованного `runtime_model["id"]`
attachments и Workspace Model owner берутся из одной записи на подготовку.
При разных IDs сохраняется отдельное чтение записи владельца capabilities
только при промахе кеша. Следующее продолжение заново проверяет active и
attachments; смена owner сама по себе не сбрасывает совпавший кеш. Для Pipe
без записи Workspace Model attachments пусты. При промахе кеша capabilities
пусты, если настроенный и runtime IDs совпадают либо запись для runtime Model ID
отсутствует. При разных IDs и активной записи для runtime Model ID сохраняется
подготовка capabilities через эту запись, включая допустимые builtin Tools.
Ранее загруженные capabilities с тем же ключом переиспользуются.
Снимок записи остаётся локальным для подготовки. История, свежий Skill context
и фильтры адресата подготавливаются в `ChildRequestBuilder`; откат и публикация
Tool context остаются в `RequestRuntime`.

Перед вызовом сабагента подготовщик оставляет в дочернем
payload только сообщения, request metadata и параметры стрима. Благодаря этому
`temperature`, `top_p`, `top_k`, `custom_params` и другие уже развёрнутые
настройки Router Model не перекрывают настройки Workspace Model сабагента.
`base_model_id`, inference params и системный промпт сабагента затем штатно
применяются провайдерным обработчиком Open WebUI.

Подготовщик отдельно подключает выбранные в карточке сабагента Tools, MCP и
builtin Tools. Для Web Search, Image Generation, Code Interpreter и
Memory дополнительно учитываются features текущего запроса, глобальные настройки
сервера, native function calling и права пользователя. Прикреплённые Knowledge
передаются builtin Tools и описываются в системном контексте. Skills обрабатывает
Subagent Preparation. Встроенные
`delegate_task` и `timer`
исключаются, чтобы сабагент не запускал параллельную систему вложенной
оркестрации поверх Lite Handoff Router.

Handoff распознаёт только актуальный маркер
`{"__lite_delegate__": "v2", "agent_id": "..."}`. Legacy-формат `v1` и поле
`skill_id` не поддерживаются.

## Контекст предыдущих инструментов

Справочная запись по умолчанию включена. В Router Preparation Valve `enabled`
можно установить в `False` (тот же Valve остаётся в самостоятельном Previous
Tool Context). В Router-цепочке даже в этом режиме стадия сохраняет
исходную историю только в request metadata: она нужна pipe для настраиваемой
истории сабагента.

Текстовый блок оркестратора содержит:

- все завершённые пары Tool Call и Tool Result предыдущего запроса;
- исходные аргументы и полные результаты без пересказа и обрезания;
- Tool executor: `orchestrator`, сабагент либо `unknown`, если принадлежность
  достоверного Tool exchange установить нельзя;
- изображения результатов, которые Open WebUI вынес в служебное user-сообщение.

Запись использует те же правила сопоставления конкретных Tool exchanges, что
Router и фильтры сабагента: неоднозначные пары и противоречивые результаты
исключаются, идентичные повторы результата сохраняются один раз. Завершённые
пары с неизвестным Tool executor остаются в справочной записи, хотя нативная
история выбранного сабагента их исключает.

Однозначный routing Skill alias подписывается каноническим Agent ID. Если
адресат удалён или alias неоднозначен, исполнитель имеет `kind: unknown`, а
исходный адресат сохраняется в `declared_agent_id`. Поля `model_id` и `name`
для подтверждённого сабагента берутся из текущего Registry и не являются
историческим снимком. Исходные Tool Calls и Tool Results сохраняются целиком.

Исторические Tools не добавляются в текущий список доступных инструментов.
Системная инструкция помечает их результаты, включая загруженные Skills и любые
вложенные инструкции, как справочные данные.

Если непосредственно предыдущий запрос не вызывал инструменты, дополнительный
блок не создаётся. Результаты более старого запроса вместо него не подставляются.

## Фильтры сабагента

Сабагент не получает строковый блок оркестратора. Перед его вызовом Router готовит
свежие capabilities, Skills и доступность loader, затем запускает прикреплённый
Subagent Preparation. Один Function выполняет следующие этапы в фиксированном
порядке:

1. Проекция Tool context удаляет вызовы и ответы инструментов, которых нет среди
   реальных Tools, MCP и builtin Tools модели адресата. Разрешение проверяется
   для каждого конкретного вызова: повторный ID не разрешает другой инструмент.
   Вызов сопоставляется с результатом только внутри своего пользовательского
   запроса и однозначной группы исполнения. Незавершённые, осиротевшие и
   неоднозначные пары не передаются модели; результаты другого запроса или
   группы не подставляются. Идентичные повторы результата сохраняются один раз.
   В текущем запросе Handoff-вызов и приватная трасса оркестратора удаляются;
   группировка вызовов Open WebUI сохраняет завершённые пары сабагента.
   Пары с неизвестным Tool executor исключаются и в текущем продолжении запроса.
   Для прошлых запросов исполнитель восстанавливается до удаления Handoff:
   переход подтверждают только реальный вызов `lite_delegate` и его однозначный
   результат v2. Прямой Agent ID и принятый routing Skill alias разрешаются
   через текущий Registry. У сгруппированных вызовов исполнитель определяется
   порядком Tool Results. Сохраняются только разрешённые завершённые пары
   выбранного сабагента; пары оркестратора, других сабагентов и пары с неизвестным
   или неоднозначным исполнителем исключаются даже при совпадении имён Tools.
   Marker-подобные данные другого Tool и legacy-маркеры не подтверждают переход.
   Текст служебной Tool-реплики, у которой исключены все исторические вызовы,
   удаляется вместе с ними и не считается итоговым ответом завершённого хода.
2. Ограничение истории выбирает последние N завершённых пар «вопрос пользователя —
   итоговый ответ модели». `history_tool_calls` служит дополнительным пределом:
   выбираются последние завершённые вызовы только внутри сохранённых текстовых
   пар, в исходном порядке. Лимит считает конкретные вызовы, включая вызовы с
   одинаковым ID, и сохраняет только их собственные результаты. Он не может
   расширить число текстовых пар. При нулевом `history_turns` или
   `history_tool_calls` исторические Tools исключаются. Обычные сохранённые
   вопросы и итоговые ответы остаются, а текущая валидная цепочка сабагента
   сохраняется независимо от исторических лимитов, в том числе на продолжениях.
   Служебные user-сообщения с изображениями Tools не начинают новый запрос.
3. Установка Skill context использует уже подготовленные Router Skills из свежих
   `skillIds` дочерней модели, прочитанных из базы перед запуском фильтра.
   При доступных builtin Tools она устанавливает manifest и добавляет
   разрешённый только для этих Skills `view_skill`, если
   есть session и builtin loader. Иначе полное содержимое Skills добавляется в
   системный промпт. Эти же правила действуют для оркестратора и обычных моделей.

В дочернем pipeline этот выбор — единственный источник прикреплённых Skills,
включая пустой список. Удаление или замена Skills в базе обновляет контекст,
собственный loader, его схему и allowlist уже при следующей подготовке в lazy
и full режимах. Устаревшие `skillIds` из runtime-кэша Open WebUI, внешнего body,
настроек Router или прежнего loader не восстанавливают отсоединённые Skills.
Вне дочернего pipeline источники выбора Skills оркестратора и обычной модели
сохраняют прежнее поведение.

Если `view_skill` становится недоступен после удаления всех Skills, перехода к
полному контексту или потери builtin loader, его нативные вызовы и результаты
исключаются уже при следующей подготовке, включая продолжение текущего запроса.
При доступном loader завершённые вызовы сохраняются по обычным правилам истории.

Идентификаторы Skills приводятся к lowercase, пробелы по краям и повторы
удаляются с сохранением порядка. Отсутствующий или неактивный Skill вызывает
ошибку. На каждом запуске подготовки Skills перечитываются, поэтому изменения
инструкций и доступности отражаются в следующем запросе. Обычные Tools и MCP
продолжают использовать request-scoped cache с прежними критериями модели и
прикреплённых Tool/Skill IDs.

Оркестратор передаёт builtin loader контекст Workspace Model owner. Для
сабагента и обычной модели используется Execution user. Allowlist ограничивает
выбор Skills, а штатный builtin сохраняет собственные проверки доступа.
Module `BuiltinSkillLoader` в `handoff_router/shared/skill_preparation.py` предоставляет
interface `load(skill_ids)`, используемый как существующий `load_builtin`
в `SkillPreparation.prepare`. На каждую подготовку создаётся loader с
`SkillBuiltinInvocation`: profile, request, runtime model, metadata, события
и OAuth; Router также передаёт стабильный Tool context и files.
Адаптер вызывающего кода выбирает пользователя через ленивый `resolve_user`.
Lookup Workspace Model owner выполняется после проверки Skills и eligibility,
только если требуется загрузить builtin. Module собирает OWUI injections,
вызывает переданный `get_builtin_tools` и применяет history adapter для Router.
Standalone сохраняет сокращённый набор injections без Router history binding.
Профили child и standalone передают `features` из metadata; orchestrator
сохраняет вызов без аргумента `features`. Eligibility, allowlist, rendering,
fallback и установка loader остаются в `SkillPreparation`; нового кеша нет.
Генератор встраивает общий module в Router, Skill Context и Subagent Preparation,
поэтому готовые Functions по-прежнему загружаются независимо.

Чужой callable или schema с именем `view_skill` вызывает явный конфликт при
подключении loader. Повторная подготовка заменяет собственный loader и Skill
context; переход к полным инструкциям или очистка Skills удаляет старый loader.

Настройки находятся в Valves **Subagent Preparation**:

| Параметр | По умолчанию | Назначение |
|---|---:|---|
| `history_turns` | `0` | Последние N прошлых пар «вопрос — ответ» |
| `history_tool_calls` | `0` | Последние N завершённых вызовов этого агента с результатами |
| `priority` | `-30` | Порядок относительно дополнительных inlet-фильтров |
| `debug` | `False` | Диагностические сообщения фильтра |

Например, `history_turns=1` и `history_tool_calls=5` сохраняют один прошлый вопрос,
итоговый ответ и не более пяти последних разрешённых завершённых Tool Calls
выбранного сабагента внутри этой текстовой пары. Обычный текст сохраняется
независимо от исполнителя, а Tool-история принадлежит конкретному сабагенту.
Текущий запрос и уже начатая Tool-цепочка сохраняются целиком независимо от лимитов.

Valves хранятся на уровне экземпляра Function. Прикрепляйте один экземпляр
Subagent Preparation ко всем сабагентам: изменение лимитов применяется к каждой
модели. Per-model overrides, policy profiles и клонирование фильтра для разных
лимитов в этой конфигурации не предусмотрены.

## Локальная проверка

Общий исходник подготовки Skills находится в `handoff_router/shared/skill_preparation.py`.
Блоки между `BEGIN GENERATED SKILL PREPARATION` и `END GENERATED SKILL PREPARATION`
в `lite_handoff_router.py`, `subagent_preparation.py` и самостоятельном
[Skill Context](../optional_filters/skill_context.py) генерируются из него. `SkillPreparation.install_context` заменяет прежний Skill
context и устанавливает loader, сохраняя инструкции Workspace Model owner;
эту операцию используют оба Skill inlet-адаптера. Меняйте общие правила в
исходнике, затем обновляйте и коммитьте готовые Function-файлы:

Команды ниже выполняются из корня репозитория.

```sh
python3 handoff_router/tools/generate_skill_preparation.py
```

Жизненным циклом запроса управляет `RequestRuntime` из
`handoff_router/shared/request_runtime.py`. Этот же генератор встраивает его в Router Preparation,
Subagent Preparation, Registry, Router и самостоятельные контекстные фильтры
в блоках `GENERATED REQUEST RUNTIME`.
Стадия Registry начинает новый запрос только после проверки конфигурации: сбрасывает
состояние предыдущего Handoff, кэши, историю, флаги и свидетельство Router-цепочки,
сохраняя живой словарь Tools. Поле `lite_router_filter_pipeline` отражает
выполненный порядок, а `lite_router_request_key` отличает повторный Registry от
нового запроса: используются платформенные `chat_id`/`message_id`, а без них —
уникальный идентификатор текущего HTTP-запроса, сохранённый в его ASGI scope.
Обёртки Request над одним scope используют тот же идентификатор; новый HTTP-запрос
получает другой даже при повторном тексте и сокращённой истории. Для адаптеров без
ASGI scope идентификатор хранится на объекте запроса. Registry устанавливает ключ
один раз; последующие фильтры и продолжения Pipe его не пересчитывают по сообщениям.
Перед отправкой модели Pipe сверяет этот ключ с идентификатором текущего запроса.
Сохранённое свидетельство прежней цепочки не заменяет Registry на новом запросе:
Pipe возвращает ошибку с указанием Registry и attachment Router Preparation,
даже если весь прежний порядок сохранён.
Previous Tool Context и History Cleanup отдельно отмечают текущий запрос в
`lite_context_filter_request_key`, включая самостоятельный запуск без Registry
и отклонённый порядок. Registry может сбросить их свидетельства на новом запросе
после прежней ошибки с пропущенным Registry; после контекстных фильтров текущего
запроса он по-прежнему отклоняется. Если у самостоятельного вызова нет ни объекта
Request, ни `message_id`, принадлежность фильтрации запросу сначала неизвестна.
Когда Request получает Pipe или Registry, он привязывает такие свидетельства к
текущему запросу до проверки пользователя, Skills и модели, а также до обратимой
подготовки модели.
Эта запись наблюдаемого запроса
сохраняется при ошибке цепочки или подготовки: поздний Registry в том же запросе
отклоняется, а новый HTTP-запрос может сбросить прежние свидетельства. Для отправки
модели по-прежнему требуется полная правильно выполненная Router-цепочка.
Все три поля входят в те же правила
синхронизации, удаления и отката, что остальные управляемые поля.
Предыдущий builtin `view_skill` и его схема удаляются только по записи владения;
чужой Tool с таким именем сохраняется.

Общий разбор Tool history находится в `handoff_router/shared/tool_history.py`. Его interface —
`analyze_history(messages, registry=...)`: результат содержит завершённые Tool
exchanges в порядке Tool Results, их Tool executor, свидетельства Handoff и
позиции пользовательских запросов. `registry=None` означает самостоятельную
модель; переданный Registry, включая пустой словарь, включает атрибуцию Router.
Разбор не изменяет входные сообщения и Registry. Позиции относятся к исходным
сообщениям, поэтому анализ выполняется до их фильтрации.

Там же находятся единый разбор v2-маркера `parse_handoff` и разрешение Agent IDs
и routing Skill aliases `resolve_agent_id`. Распознавание текущего Handoff
не зависит от доступности адресата: неизвестный Agent ID по-прежнему приводит
к ошибке подготовки дочернего запроса. Политики выбора истории, ограничения Tools
и оформление справочной записи остаются отдельно от разбора фактов.

Генератор встраивает общий разбор в Router, Previous Tool Context, Tool Call
Filter, Subagent Context, Router Preparation и Subagent Preparation в блоках `GENERATED TOOL HISTORY`.
Общий исходник нужен только для разработки: каждый готовый Function по-прежнему
загружается независимо.

Выбор и реконструкция Tool context находятся в `handoff_router/shared/tool_context.py`.
Module `ToolContextProjection` предоставляет две независимые операции:
`available_tools(body)` выбирает допустимые завершённые Tool exchanges и
возвращает сообщения с набором разрешённых имён Tools для диагностики;
`completed_history(messages, history_turns=..., history_tool_calls=...)` выбирает
завершённые текстовые ходы и применяет дополнительный лимит Tool exchanges.
Обе операции используют общий renderer: он сохраняет порядок конкретных вызовов
и результатов, восстанавливает grouped calls после Handoff и различает
историческую Tool narration, итоговый ответ и текущую Tool-цепочку.

Операции не изменяют входные сообщения или metadata и не сохраняют состояние
между вызовами. Каждая заново анализирует свои входные сообщения: после Tool Call
Filter исходные позиции уже изменены. Subagent Context может вызываться отдельно,
без предварительного ограничения Tools. Проверки входного body, Valves, порядок
фильтров и lifecycle trace остаются в inlet adapter-ах. Генератор встраивает
общий module в Tool Call Filter, Subagent Context и Subagent Preparation в блоках
`GENERATED TOOL CONTEXT`.

Подготовка оркестратора и дочерней модели выполняется в обратимом переходе.
При ошибке до отправки модели восстанавливаются управляемые поля metadata,
состав Tools и история, с сохранением ссылок на словарь и список. Отдельный
`request.state.metadata` получает эти изменения, включая удаления, а его
платформенные поля сохраняются. Откат не отменяет внешние эффекты фильтров и
подключения MCP: новые клиенты остаются в ресурсном реестре. Успешная подготовка
фиксируется до status/completion; ошибка провайдера её не отменяет и не запускает
повторную попытку.

Interface `RequestRuntime.prepare_model(branch, body)` объединяет эту подготовку
с жизненным циклом model-bound capabilities. Ветки `orchestrator` и `child` имеют
отдельные стабильные списки Tool context на весь запрос Execution user.
`prepared.capabilities(model_id=..., tool_ids=..., skill_ids=..., load=...)`
переиспользует кеш при совпадении модели и нормализованных attachment IDs либо
вызывает загрузчик со стабильным списком сообщений. Смена модели или attachments
пересобирает Tools, сохраняя список; новый запрос сбрасывает оба кеша и списка.

После успешного выхода из блока список получает копию окончательных сообщений
`prepared.body`, передаваемых в `CompletionGateway`, включая Skill context и
результат всех inlet filters. До этого загрузчики видят предыдущий успешный
Tool context, а при первой подготовке — пустой список. Копия отделяет историю
Tools от body, отправляемого модели. Публикация происходит до status/completion;
ошибка подготовки восстанавливает прежние содержимое и ссылки.

`ModelCapabilityResolver` сохраняет этот Tool context и при выполнении через
штатную callable-обёртку Open WebUI. Native Tool loop Open WebUI 0.11.1 подставляет
в `__messages__` внешнюю историю Router, которая отличается от подготовленной
истории выбранной модели и остаётся прежней при внутренних продолжениях.
Adapter закрепляет `__messages__` для загруженных Router Tools; signature, schema,
преобразование аргументов и стандартное обновление `__files__` сохраняются.
При валидном reuse сохраняются callable и MCP clients.

Перед загрузкой Tools и на каждом продолжении Pipe устанавливает
`metadata["model_id"]` в ID выбранной Workspace Model сабагента, а для
оркестратора — в настроенный `orchestrator_model_id`. Значение синхронизируется
с `request.state.metadata`, включая случай разных словарей: штатный OWUI
при вложенном вызове объединяет request-state metadata поверх payload metadata.
Кешированные Tools, захватившие прежний словарь метаданных, получают актуальный
`model_id` после успешной подготовки; их callable сохраняется.
Tool может использовать этот ID в `form_data["model"]` при вызове
`open_webui.utils.chat.generate_chat_completion`, сохраняя обычное разрешение
Workspace Model в базовую LLM, её inference params и системный промпт.
Такой вызов не отправляется обратно в Router из-за унаследованного Pipe ID.
При ошибке подготовки прежнее значение либо отсутствие `model_id`
восстанавливается отдельно в обоих словарях. После успешной подготовки
выбор сохраняется, в том числе при последующей ошибке провайдера.
Обычные контекстные фильтры сами `model_id` не переопределяют.

При обновлении комплекта до **0.20.0** обновите все семь Functions с блоком
`GENERATED REQUEST RUNTIME`: Router, Registry, Skill Context, Previous Tool
Context, History Cleanup, Tool Call Filter и Subagent Context.
При обновлении с 0.20.0 до **0.20.1** достаточно обновить Tool Call Filter:
исправлено исключение пар с неизвестным Tool executor в текущем запросе.
При обновлении до **0.20.2** обновите Tool Call Filter и Subagent Context:
выбор и реконструкция истории теперь используют общий module без изменения
правил истории, priorities или Valves.
При обновлении с 0.20.2 до **0.20.3** достаточно обновить Router:
подготовка Workspace Model использует единый свежий снимок записи при совпадающих
IDs, с прежней политикой кеша и совместимостью разных IDs.
При обновлении с 0.20.3 до **0.20.4** обновите Router и Skill Context:
общий builtin Skill loader сохраняет пользователей, injections и историю
каждого пути, с прежними Skill-политиками и свежей загрузкой.
При обновлении до **0.21.0** используйте замену attachments обеих ролей из
раздела установки: Router Preparation на Router, общий Subagent Preparation
на адресатах. Все девять Functions с `GENERATED REQUEST RUNTIME` актуальны;
для самостоятельных обычных моделей обновите соответствующие контекстные
фильтры из `optional_filters/`.
При обновлении с 0.21.0 до **0.21.1** достаточно обновить Pipe из
`lite_handoff_router.py`: исправлена модель для вложенных запросов из Tools.
Настройки, оба Preparation attachment-а и `lite_delegate` сохраняются.

В `optional_filters/lite_subagent_registry.py` и `router_preparation.py`
генератор встраивает только функцию
нормализации Skill IDs из общего исходника: Registry проверяет Skills до вызова
Pipe, поэтому его lookup должен использовать те же канонические IDs. Изменение
этой функции требует регенерации и коммита всех пяти файлов. Tool и model IDs
сохраняют исходный регистр.

Авторитетные исходники Router-стадий находятся в `handoff_router/shared/registry_preparation.py`,
`handoff_router/shared/previous_tool_context.py` и `handoff_router/shared/history_cleanup.py`. Генератор встраивает
их в Router Preparation и соответствующие самостоятельные адаптеры. Меняйте
логику стадий в этих исходниках; каталог `SUBAGENTS`, Valves и composition inlet
остаются в коде соответствующей Function. Готовый `router_preparation.py`
загружается отдельно, без соседних Python-модулей.

Код Router и Filter вне этих блоков редактируется напрямую. Генератор сохраняет
его и при неизменном исходнике не переписывает файлы. Для проверки актуальности
без записи используйте `--check`; stale output завершает команду с кодом `1`.
Пользователям Open WebUI по-прежнему достаточно загрузить готовые Function-файлы:
генератор и общий исходник при установке не нужны.

Команды ниже выполняются из корня репозитория.

```sh
python3 handoff_router/tools/generate_skill_preparation.py --check
python3 -B -m unittest discover -s handoff_router/tests -v
python3 -B -m unittest discover -s deployment -p 'test_*.py' -v
```

Тесты не требуют работающего сервера, провайдера модели или MCP. Они проверяют
порядок фильтров, idempotency, построение Skill manifest, отсутствие дублирования
переписки, изоляцию контекста сабагента и продолжение нескольких Tool Calls.
Сценарии Router вызывают публичный `Pipe.pipe` с реальными контекстными фильтрами
и подменяют только внешние операции Open WebUI. Проверки cache охватывают
переиспользование capabilities, смену модели и прикреплённых Tools/Skills,
обновление истории в cached callables и сохранение общих Tools и metadata.
Проверки Tool context охватывают обе ветки Router, окончательные сообщения после
Skills и фильтров, публикацию до status, rollback и native callable refresh OWUI.
Для последнего используется локальный stand-in внешней обёртки с её правилами
binding и refresh: устаревшая внешняя история не заменяет подготовленный контекст,
а `__files__` продолжает обновляться для синхронных и асинхронных Tools.
Одна матрица Skill-политики проверяет `Pipe.pipe` и `Filter.inlet`: lazy/full
eligibility, fallback, canonical IDs, ownership, allowlist, конфликты и freshness.
Тесты генератора проверяют read-only freshness check, воспроизводимость,
сохранение независимого кода и импорт каждого Function без соседних модулей.
`test_subagent_preparation.py` запускает реальные Router inlets и `Pipe.pipe`
с единственным Subagent Preparation у адресата: общие лимиты двух моделей,
Tool/executor isolation, Skills freshness, loader eligibility, callable-visible
контекст после дополнительных фильтров, продолжения и rollback.
`test_preparation_integration.py` выполняет оба новых фильтра вместе: Router
получает только Router Preparation, а адресаты — общий Subagent Preparation.
Проверяются переход оркестратор → Handoff, справочная запись и сохранение
источника при её отключении, общие лимиты двух моделей, свежий выбор Skills,
продолжения, сброс нового запроса и восстановление при ошибках.
Регрессии повторных Tool Call ID проверяют разрешение каждого вызова,
однозначность пары и исторические лимиты через `Pipe.pipe` и `Filter.inlet`.
