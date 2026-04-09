# Contributing to BamDude

Thank you for your interest in contributing! This document provides guidelines for BamDude (hard fork of [Bambuddy](https://github.com/maziggy/bambuddy)).

## Table of Contents

- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Code Style](#code-style)
- [Internationalization (i18n)](#internationalization-i18n)
- [Telegram Bot Development](#telegram-bot-development)
- [Authentication & Permissions](#authentication--permissions)
- [Testing](#testing)
- [Submitting Changes](#submitting-changes)

## Getting Started

1. **Clone the repository**:
   ```bash
   git clone https://github.com/kainpl/bamdude.git
   cd bamdude
   ```

2. **Create a branch** from `dev`:
   ```bash
   git checkout dev
   git checkout -b feature/your-feature-name
   ```

## Development Setup

### Prerequisites

- Python 3.10+ (3.11/3.12 recommended)
- Node.js 20+
- npm

### Backend

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Install pre-commit hooks
pip install pre-commit
pre-commit install

# Run backend
DEBUG=true uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev  # http://localhost:5173, proxies /api to backend:8000
```

## Making Changes

### Branch Naming

- `feature/` — New features
- `fix/` — Bug fixes
- `docs/` — Documentation
- `refactor/` — Code refactoring
- `test/` — Test additions

### Commit Messages

Use conventional commits: `feat:`, `fix:`, `chore:`, `refactor:`, `docs:`, `test:`

## Code Style

### Backend (Python)

[Ruff](https://github.com/astral-sh/ruff) for linting and formatting (config in `pyproject.toml`):
- Line length: 120 chars
- Double quotes, space indentation
- Target Python 3.10

```bash
ruff check backend/          # lint
ruff check --fix backend/    # lint + autofix
ruff format backend/         # format
```

### Frontend (TypeScript/React)

ESLint flat config + strict TypeScript:

```bash
cd frontend
npm run lint      # eslint
npx tsc --noEmit  # type check
```

### Pre-commit Hooks

Run automatically on `git commit`. Manual run:

```bash
pre-commit run --all-files
```

## Internationalization (i18n)

### Frontend (React)

All user-facing strings must use `useTranslation()` — never hardcode strings.

Locale files in `frontend/src/i18n/locales/`:

| File | Language |
|------|----------|
| `en.ts` | English |
| `uk.ts` | Ukrainian |

**All locales must have identical key structure.** Add keys to BOTH files.

```tsx
import { useTranslation } from 'react-i18next';

function MyComponent() {
  const { t } = useTranslation();
  return <span>{t('section.myKey')}</span>;
}
```

### Backend (Telegram Bot)

Bot UI strings are in `backend/app/data/telegram_ui_{lang}.json`. Accessed via:

```python
from backend.app.i18n import t, get_language, escape_md

lang = await get_language()
text = t(lang, "telegram_ui", "printers.title")
```

- Dot-path key lookup with fallback to English
- `escape_md(text)` for MarkdownV2 special characters
- Adding a new language: create `telegram_ui_{XX}.json`, add to `SUPPORTED_LOCALES` in `locale_updater.py`

### Notification Templates

JSON files in `backend/app/data/`:
- `notification_templates_{en,uk}.json`
- `maintenance_types_{en,uk}.json`

Updated automatically when system language changes via `locale_updater.py`.

## Telegram Bot Development

### Architecture

```
backend/app/services/
  telegram_bot.py              # Bot singleton, polling, lifecycle
  telegram_handlers/
    auth_middleware.py          # Chat authorization middleware
    start.py                   # /start, /help, /status, reply keyboard
    printers.py                # Printer list, detail, controls, maintenance, hours
```

### Key Patterns

**Parse mode:** MarkdownV2. All dynamic content must be escaped:
```python
from backend.app.i18n import escape_md
name = escape_md(printer["name"])
text = f"*{name}*"
```

**Permissions:** Handlers receive `tg_chat` from middleware:
```python
@router.callback_query(F.data.startswith("action:"))
async def handler(callback: CallbackQuery, tg_chat: TelegramChat | None = None):
    if not _has_perm(tg_chat, "printers:control"):
        await callback.answer("No permission", show_alert=True)
        return
```

**FSM for multi-step input** (e.g., hours editing):
```python
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

class MyState(StatesGroup):
    waiting = State()
```

**Callback data convention:** `{category}:{action}:{params}` (e.g., `action:pause:5`, `maint:done:12:5`)

**Actionable notifications:** Add buttons in `notification_service._build_telegram_actions()`.

## Authentication & Permissions

Auth is opt-in. Endpoints use `RequirePermissionIfAuthEnabled`:

```python
from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.permissions import Permission

@router.get("/my-resource")
async def get_resource(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.RESOURCE_READ),
):
    ...
```

Permissions follow `resource:action` pattern. New permissions go in `backend/app/core/permissions.py` + `PERMISSION_CATEGORIES` + default groups.

| Group | Access Level |
|-------|-------------|
| **Administrators** | All permissions |
| **Operators** | Full control, own items |
| **Viewers** | Read-only |

## Testing

```bash
./test_frontend.sh    # tsc + eslint + vitest
./test_backend.sh     # ruff + pytest
./test_all.sh         # frontend + backend + docker + security
./test_docker.sh      # Docker build + tests
./test_security.sh    # bandit, pip-audit, npm-audit
```

Individual:

```bash
# Backend
pytest backend/tests/ -v
pytest backend/tests/unit/
pytest backend/tests/ --cov=backend

# Frontend
cd frontend
npm run test:run
npm run test:coverage
```

## Submitting Changes

1. Push your branch and create a PR targeting `dev` (not `main`)
2. Use a clear title and fill out the PR description
3. Include screenshots for UI changes
4. Ensure all tests pass

### PR Guidelines

- One feature or fix per PR
- Update docs if needed (CLAUDE.md, temp/ feature docs)
- Add i18n keys to ALL locales
- Test Telegram bot changes with a real bot

---

Thank you for contributing to BamDude!
