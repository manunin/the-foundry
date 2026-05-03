# AGENTS.md — правила проекта для агентов

Этот файл читается агентами (plan / implement / verify) в начале каждой задачи. Следуй правилам ниже — они отражают конвенции кодовой базы и требования пайплайна.

---

## Стек и соглашения

- **Python 3.11+**, форматтер `ruff`. Импорты — абсолютные от пакета (`from foundry.models import ...`), не относительные.
- **TypeScript** в `web/` — strict mode, без `any`, компоненты в `web/src/components/`.
- Типизация обязательна: аннотируй все публичные функции и dataclass-поля.
- `from __future__ import annotations` — в каждом Python-файле, где есть аннотации.
- Enum-строки через `StrEnum` (не `str, Enum`).
- Новые зависимости — только при реальной необходимости; добавлять через `uv add`.

## Структура репозитория

```
src/foundry/          # ядро пайплайна (Python)
  agents/             # coding-агенты: base, claude_cli, codex_cli, opencode_cli, stub
  stages/             # стадии FSM: fetch, context, plan, agent_plan, agent_implement, verify, pr
  models.py           # SQLite-схема (Task, TaskEvent, RepoMemory …)
  state.py            # CRUD поверх SQLite
  workflows.py        # FSM-логика, retry, pr_feedback
  security.py         # assert_command_allowed, scrubbed_agent_env, sanity_check_changes
  events.py           # append-only event log
src/api/              # FastAPI HTTP API
web/                  # React + Vite UI
tests/                # pytest-тесты
```

## Что можно и нельзя менять агенту

**Разрешено:**
- Создавать/изменять файлы в своей рабочей директории (git worktree задачи).
- Читать любые файлы внутри worktree.

**Запрещено:**
- Выходить за пределы cwd: никаких `../`, абсолютных путей наружу, обращений к родительскому репозиторию.
- `git commit`, `git push`, создание/переключение веток — это делает оркестратор.
- Устанавливать пакеты без явной необходимости для задачи.
- Изменять более 40 файлов в одной задаче (`MAX_FILES_PER_PR`).
- Трогать `security.py`, `worktree.py`, `.env` — без явного указания в задаче.

## Стиль кода

- Комментарии только там, где WHY неочевиден. Никаких «этот метод делает X».
- Имена говорящие: `task_id`, `worktree_path`, `stage_result` — не `t`, `p`, `r`.
- Обработка ошибок — только на границах системы (внешние API, subprocess). Внутренний код доверяет инвариантам.
- Тесты: `tests/` с pytest. Новая фича — новый тест. Имя теста описывает сценарий: `test_fetch_skips_closed_issues`.

## Верификация

После реализации агент-ревьюер запустит:
- `ruff check .` — линтинг Python
- `pytest` — тесты

Убедись, что изменения проходят оба. Если добавляешь зависимость — она должна быть в `pyproject.toml`.

## Сигналы для оркестратора

Если задача неоднозначна и без уточнений продолжать небезопасно — добавь в конец ответа:
```
NEED_VERIFICATION
<список вопросов>
```
Оркестратор заблокирует задачу и опубликует вопросы в GitHub issue.
