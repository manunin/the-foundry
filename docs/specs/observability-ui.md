# Observability & Live UI

## UI_TESTS contract

The API exposes `ui_tests_enabled` on every task. When false, clients omit the
`ui_tests` timeline entry. When true, `ui_tests` appears between `verify` and
`pr` and is agent-backed for live event streaming without the ask-agent
composer.

`stage_started.input` includes the attempt, deploy-skill path, plan summary, and
artifact limits. `stage_finished.output` uses the normal quality-gate fields
plus `deployed_url`, scenarios, bounded `core_logs`, `ui_logs`, `browser_logs`,
and screenshot metadata. Each screenshot contains an opaque `url`; no
filesystem path is projected.

`GET /api/tasks/{task_id}/artifacts/{artifact_path}` returns only an image listed
by a persisted UI_TESTS result for that task. Traversal, cross-task paths,
missing files, unsupported types, and unlisted files return 404. The Output tab
shows aggregate counts, scenarios, collapsible diagnostics, and lazy-loaded
screenshot links while retaining the generic payload fallback.

Актуальный контракт observability-слоя Foundry. Источник архитектурного
контекста — [../ARCHITECTURE.md](../ARCHITECTURE.md).

## Что Есть

Foundry пишет операционную историю задач в append-only таблицу `task_events`.
Эта таблица — источник истины для API и UI; `tasks.logs_json` остаётся
legacy-дневником для совместимости и быстрых отладочных записей.

Каждая задача может эмитить:

- `stage_started`
- `stage_finished`
- `stage_failed`
- `agent_tool`
- `agent_thinking`
- `agent_text`
- `agent_result`
- `agent_span_started`
- `agent_span_finished`
- `agent_span_failed`

События имеют монотонный `seq` внутри `task_id`, поэтому SSE может безопасно
replay'ить хвост после reconnect через `Last-Event-ID`.

## Backend API

Проекция задачи сохраняет совместимое поле `pr_url` и дополнительно отдаёт
`forge`, `forge_host`, `issue_url` и `change_kind` (`PR` или `MR`). UI использует
готовый `issue_url` и не конструирует provider-specific ссылки. Событие
change-request feedback содержит компактные `forge`, `change_number`,
`change_url` и `feedback_fingerprint`; полные ответы forge API не сохраняются.
При возобновлении blocked-задачи событие `human_clarification_received`
содержит только external comment IDs и authors; текст ответа поступает в task
context, но полный forge API response в event log не записывается.

FastAPI живёт в `src/api/`.

| Endpoint | Назначение |
| --- | --- |
| `GET /api/tasks` | Список задач с агрегированными стадиями, без полного event stream. |
| `GET /api/tasks/{id}` | Детали задачи и последние 200 events. |
| `GET /api/tasks/{id}/events` | SSE stream, replay из SQLite + live events. |
| `POST /api/tasks/{id}/reset` | Вернуть не-running задачу в `pending/fetch`. |
| `POST /api/tasks/{id}/resume` | То же, но semantic action для `blocked` задач после human answer. |
| `GET /api/repos` | Счётчики задач по репозиториям и статусам. |
| `GET /api/repos/{repo}/memory` | Repo memory (`touched_files`, `verify_commands`, `common_failures`, PR feedback hashes). |

Проекция находится в `src/api/projections.py`. Внутренние стадии остаются
`plan`/`implement`; для UI они алиасируются в `agent_plan`/`agent_implement`.

## Frontend UI

React/Vite приложение живёт в `web/`.

Текущие ключевые элементы:

- sidebar с репозиториями и counts;
- topbar и filter bar;
- таблица задач;
- status chips;
- stage stepper;
- expandable task details;
- stage input/output panel;
- live event stream;
- disabled ask-agent composer как UI-заготовка.

UI получает список задач через polling (`GET /api/tasks`) и stream конкретной
раскрытой задачи через `EventSource`.

## Event Payload Guidelines

Короткие поля (`tool`, `detail`, `summary`, `error`, `model`) должны оставаться
читаемыми verbatim. Длинные поля (`text`, `stdout`, `stderr`, `input`, `output`)
режутся в `src/foundry/events.py`, чтобы SQLite и UI не захлебнулись большими
ответами агента.

Для агентских tool events нормализация делается в
`src/foundry/agents/streaming.py`: `Read` показывает путь, `Bash` — description
или command, `Grep` — pattern, неизвестные инструменты показываются безопасно
без обязательного `detail`.

Timing spans образуют иерархию `run → attempt → turn/tool`; rate-limit backoff
записывается отдельным span. Длительности считаются через monotonic clock,
а `ts_ms` остаётся wall-clock временем события. Если backend прислал только
завершение tool event, `duration_ms` остаётся `null`, чтобы не создавать
ложную точность.

Полная история доступна страницами через
`GET /api/tasks/{id}/event-history?before_seq=<seq>&limit=<n>`. Обычный task
detail по-прежнему возвращает последние 200 events.

## Открытые Следующие Шаги

- Показать агрегированную стоимость и токены по задаче/дню/репозиторию.
- Подключить composer к реальному agent resume/chat flow.
- Добавить management actions за пределами reset/resume: cancel, retry with
  reason, manual run.
- Добавить auth, если UI выйдет за пределы localhost.
- Уйти от SQLite polling в сторону более явного pub/sub, если live-load станет
  заметной нагрузкой.
