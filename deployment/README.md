# Open WebUI 0.11.1 — проверенная локальная установка

Проверено 9 сентября 2026 года.

- URL: http://localhost:3000
- Контейнер: `open-webui`, состояние `healthy`, restart policy `always`.
- Образ: `ghcr.io/open-webui/open-webui:v0.11.1`.
- Загруженный digest: `sha256:6bb1fbe8ab0a3e0456067f493044ffb66a30a65a34be47f6a5862176a370dd16`.
- `/api/version`: `{"version":"0.11.1","deployment_id":""}`.
- Существующий внешний volume `open-webui` подключён к `/app/backend/data`.
- Сохранено `HF_HUB_OFFLINE=1`. Авторизация и защита iframe не отключались.
- Compose-меток у исходного контейнера не было; compose в проекте, Documents и `.config` не найден.

## Запуск

```sh
docker compose -f /Users/kirill/Documents/ChatGPT/owui/deployment/compose.yaml up -d
```

Настройки OpenRouter находятся в существующем volume. Ключ не включён в compose
или Tool. Значения всех `openai.*` в базе после обновления совпали с резервной
копией. Проверка SQLite `PRAGMA quick_check` вернула `ok` для текущей базы и копии.

## Резервная копия и аккаунт

Полная копия `/app/backend/data` создана при остановленном сервере:
`backups/open-webui-before-v0.11.1-20260909.tar.gz` (238 MiB).

SHA-256: `67124c5c341a1b1437f8fd03e55aee67e0e48ffcde0b244dbb7d4cc44364657c`.

Архив включает SQLite DB, WAL и SHM, загрузки и остальные данные volume.
При восстановлении нужно восстанавливать весь архив в отдельный чистый volume;
нельзя брать только `webui.db`, игнорируя WAL. Откат версии требует восстановления
данных из этой копии, а не запуска старого образа поверх мигрированной базы.
Старый контейнер оставлен остановленным как `open-webui-before-v0.11.1-20260909`,
его автоматический перезапуск отключён; он всё ещё ссылается на рабочий volume.

По просьбе пользователя пароль единственного существующего администратора
заменён случайным паролем с хешем Argon2. Адрес входа и новый пароль сохранены
в `backups/local-login.json` с правами `0600`, каталог `backups` имеет права `0700`
и исключён из Git. Аккаунт и его данные сохранены. Пароль также можно заменить
самостоятельно в настройках аккаунта. Файл с данными входа и резервная копия
содержат приватные данные и не предназначены для публикации.

## Rich UI

- Чат: http://localhost:3000/c/c880de77-eadc-4449-ba9e-b6aaf6dfa82f
- Модель: `openai/gpt-4.1-mini` через существующий OpenRouter.
- Инструмент: Workspace → Tools → **Rich UI Demo**, ID `rich_ui_demo`.
- Код: `rich_ui_demo.py` — точная копия сохранённого в Open WebUI Tool.
- Режим вызова функций: **Нативный**, выбран явно в параметрах этого чата.
- `max_tokens=1024` для короткой демонстрации. Первая попытка с лимитом
  провайдера 65536 была отклонена из-за доступного баланса; повторная генерация
  с 1024 завершилась успешно. Ошибка осталась в альтернативной ветке ответа 1/2;
  открыта успешная ветка 2/2.
- Модель фактически вызвала `show_rich_ui`, результат отобразился как `srcdoc` iframe.
- Нажатие **Обновить данные +1** проверено через браузер: счётчик 0 → 1,
  всего заявок 150 → 170, столбцы [18,27,23,38,44] → [20,30,27,43,50].
- Снимок результата: `rich-ui-verified.png`.
- Фактический sandbox: `allow-scripts allow-popups allow-downloads`.
  `allow-same-origin` отсутствует. Высота автоматически установилась в 499 px.
- Виджет использует только встроенные HTML/CSS/JavaScript/SVG. Сетевых запросов нет.
- Счётчик демонстрационный и хранится в памяти iframe: при перезагрузке чата
  возвращается к 0; сохранённый вызов инструмента и сам виджет остаются в чате.

## Проверенные исходники именно тега v0.11.1

- [Обработка HTMLResponse + Content-Disposition inline](https://github.com/open-webui/open-webui/blob/v0.11.1/backend/open_webui/utils/middleware.py#L1023).
- [FullHeightIframe: sandbox и сообщение iframe:height](https://github.com/open-webui/open-webui/blob/v0.11.1/src/lib/components/common/FullHeightIframe.svelte).
- [Отображение результата tool call](https://github.com/open-webui/open-webui/blob/v0.11.1/src/lib/components/chat/Messages/Markdown/ConsecutiveDetailsGroup.svelte).

Скачан и прочитан официальный архив тега v0.11.1. Метод возвращает
`HTMLResponse(content=html, headers={"Content-Disposition": "inline"})`.
Высота передаётся через `parent.postMessage({type: 'iframe:height', height}, '*')`;
родитель проверяет источник сообщения на равенство окну своего iframe.
