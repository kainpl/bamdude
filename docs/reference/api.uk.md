---
title: Довідка API
description: Посібник з інтеграції REST API BamDude — автентифікація, права, rate limits, групи endpoint-ів та рецепти.
---

# Довідка API

BamDude надає версіоноване REST API за адресою `/api/v1` плюс канал WebSocket для подій принтерів у реальному часі. Усе, що робить веб-інтерфейс, доступне і вашим скриптам та інтеграціям.

---

## :material-rocket-launch: Швидкий старт

- **Базовий URL:** `https://<your-bamdude-host>/api/v1`
- **Інтерактивна документація:** [`/docs`](#) (Swagger UI) та [`/redoc`](#) (ReDoc)
- **OpenAPI-схема:** `/openapi.json` — підключайте до Postman, Insomnia або будь-якого OpenAPI-сумісного клієнта
- **Канал реального часу:** `wss://<your-bamdude-host>/api/v1/ws`

Усі endpoint-и повертають JSON, якщо не вказано інше (потоки камери, завантаження 3MF та мініатюри повертають бінарні дані). Помилки мають форму FastAPI:

```json
{ "detail": "Not authenticated" }
```

Помилки валідації `422` повертають масив проблем рівня поля:

```json
{
  "detail": [
    { "loc": ["body", "name"], "msg": "field required", "type": "value_error.missing" }
  ]
}
```

!!! tip "Спочатку відкрийте `/docs`"
    Інтерактивний Swagger UI генерується з живого сервера, тому завжди відображає маршрути, схеми та потрібні права тієї версії, що у вас запущена. Цю сторінку сприймайте як орієнтир; джерелом істини вважайте `/docs`.

---

## :material-key: Методи автентифікації

BamDude підтримує два механізми автентифікації. Обидва застосовують однакові перевірки прав. API-ключі звіряються першими; якщо жоден заголовок не присутній, запит проходить далі до JWT.

=== "API-ключ (рекомендовано для скриптів)"

    Створіть ключі в **Налаштування → Система → API-ключі**. Вони мають вигляд `bb_<random_token>` і ніколи не закінчуються автоматично — відкликайте їх по одному, коли вони більше не потрібні.

    Надсилайте через будь-який заголовок:

    ```bash
    # Preferred: dedicated header
    curl -H "X-API-Key: bb_abc123..." \
      https://bamdude.example.com/api/v1/printers/

    # Equivalent: bearer scheme
    curl -H "Authorization: Bearer bb_abc123..." \
      https://bamdude.example.com/api/v1/printers/
    ```

    Кожен ключ має власний набір прав — підмножину прав користувача, що його випустив. Видалення користувача анулює його ключі.

=== "JWT session token (використовується веб-інтерфейсом)"

    Браузерний потік:

    ```bash
    curl -X POST -H "Content-Type: application/json" \
      -d '{"username": "admin", "password": "...", "remember_me": true}' \
      https://bamdude.example.com/api/v1/auth/login
    ```

    Відповідь:

    ```json
    {
      "access_token": "eyJhbGc...",
      "token_type": "bearer",
      "requires_2fa": false,
      "user": { "id": 1, "username": "admin", "...": "..." }
    }
    ```

    Надсилайте access-токен через `Authorization: Bearer <jwt>`. **Access-токени живуть 1 годину.** Refresh-токен встановлюється як HttpOnly cookie (`bamdude_refresh`) на шляху `/api/v1/auth` — викликайте `POST /api/v1/auth/refresh`, щоб прозоро отримати новий access-токен. Атрибути cookie:

    | Атрибут | Значення |
    |---------|----------|
    | Path     | `/api/v1/auth` (ваш клієнт повинен це зберігати) |
    | HttpOnly | так — ніколи не доступний JavaScript |
    | SameSite | `Lax` |
    | Secure   | автовизначення зі схеми запиту; враховує `X-Forwarded-Proto` за довіреним проксі. Примусово встановлюється env-змінною `AUTH_REFRESH_COOKIE_SECURE`. |
    | Max-Age  | 30 днів коли `remember_me=true`; інакше session cookie + 12 год часу життя в БД |

    !!! warning "Потік 2FA"
        Коли `requires_2fa: true`, відповідь login також містить `pre_auth_token` та cookie 2FA-челенджу. Надішліть POST на `/api/v1/auth/2fa/verify` разом з TOTP-кодом користувача (або резервним кодом), щоб отримати access + refresh токени.

    OIDC SSO працює за тим самим шаблоном через `/api/v1/auth/oidc/exchange` (PKCE S256 + state + nonce).

---

## :material-speedometer: Rate limiting

BamDude обмежує частоту запитів на endpoint-ах автентифікації, щоб уповільнити credential stuffing. Інші endpoint-и не мають rate limit на рівні API — поставте перед ними реверс-проксі або фаєрвол, якщо потрібна жорстка межа.

| Endpoint | На користувача / email | На IP |
|----------|------------------------|-------|
| `POST /auth/login` | 10 / 15 хв на ім'я користувача | 20 / 15 хв |
| `POST /auth/forgot-password` | 3 / 15 хв на email | 10 / 15 хв |

Коли ліміт спрацьовує, ви отримуєте `429 {"detail": "..."}` та заголовок `Retry-After`.

!!! tip "За реверс-проксі"
    Встановіть `TRUSTED_PROXY_IPS` (через кому довірені хопи), щоб rate limit зчитував реальний IP клієнта з `X-Forwarded-For`, а не IP проксі. Повні рецепти для nginx / Caddy / Traefik див. у [Реверс-проксі та HTTPS](../getting-started/reverse-proxy.md).

---

## :material-shield-lock: Setup-шлюз

Під час першого запуску сервер приймає лише три endpoint-и. Усі інші запити повертають `503 {"detail": "setup_required"}`, доки не буде створено адміністратора.

| Endpoint | Призначення |
|----------|-------------|
| `GET  /api/v1/auth/status` | Повертає `{is_setup, requires_setup, ...}`, щоб інсталятори могли визначити стан порожньої БД. |
| `POST /api/v1/auth/setup`  | Одноразовий: створює початкового адміна та повертає access + refresh токени. |
| `GET  /api/v1/system/health` | Перевірка живучості (завжди в білому списку). |

Після завершення setup шлюз вимикає себе у пам'яті процесу; перезапуск не потрібен.

!!! danger "Втратили всіх адмінів?"
    Виконайте `python -m backend.app.cli reset_admin` на сервері, щоб скинути setup-прапорці, потім зайдіть в інтерфейс і пройдіть setup ще раз. Повний протокол відновлення див. у [Відновлення автентифікації](../features/authentication.md).

---

## :material-lock-check: Права

Кожен endpoint захищено через `RequirePermission(Permission.X)`, де `X` слідує шаблону `resource:action`. У `backend/app/core/permissions.py` визначено **80+ прав**. Поширені:

| Ресурс | Приклади |
|--------|----------|
| Printers | `printers:read`, `printers:control`, `printers:create`, `printers:delete`, `printers:files`, `printers:clear_plate` |
| Archives | `archives:read`, `archives:create`, `archives:update_own`, `archives:update_all`, `archives:delete_own`, `archives:delete_all`, `archives:reprint_own`, `archives:reprint_all` |
| Library  | `library:read`, `library:upload`, `library:update_own`, `library:delete_all`, `library:notes_write` |
| Queue    | `queue:read`, `queue:create`, `queue:update_all`, `queue:delete_all`, `queue:reorder` |
| Users    | `users:read`, `users:create`, `users:update`, `users:delete` |
| Settings | `settings:read`, `settings:update`, `settings:backup`, `settings:restore` |
| Camera   | `camera:view` |

Три типові групи покривають більшість сценаріїв:

- **Administrators** — усі права.
- **Operators** — повний контроль над принтерами, чергою, архівами, бібліотекою; без адміністрування налаштувань / користувачів.
- **Viewers** — лише читання.

Створюйте власні групи для тонкого контролю. Інтерактивний браузер `/docs` показує необхідне право для кожного endpoint-у.

---

## :material-routes: Групи endpoint-ів

43 модулі маршрутів у `backend/app/api/routes/` зареєстровані під префіксом `/api/v1`. Основні групи на одному екрані:

| Префікс | Що робить | Помітні endpoint-и |
|---------|-----------|--------------------|
| `/auth/*` | Логін, refresh, setup, OIDC | `login`, `refresh`, `logout`, `setup`, `2fa/verify`, `oidc/exchange`, `forgot-password` |
| `/users/*`, `/groups/*`, `/api-keys/*`, `/mfa/*` | Користувачі, групи, API-ключі, реєстрація MFA | CRUD, призначення груп, скидання MFA, резервні коди |
| `/printers/*`, `/printer-queues/*`, `/cloud/*`, `/discovery/*` | Принтер + AMS + Bambu Cloud | статус, керування, RFID AMS, snapshot, stream-токен, мережеве виявлення |
| `/archives/*` | Історія друку | список, отримання, передрук, видалення, **`retry-download`**, **`cleanup/preview`**, **`cleanup/run`**, **`cleanup/status`** |
| `/queue/*`, `/background-dispatch/*` | Керування чергою + dispatch | додавання, перевпорядкування, скасування, set-status, стан dispatch-у |
| `/library/*`, `/library-notes/*`, `/pending-uploads/*` | Файловий менеджер | завантаження, список, видалення, додавання в чергу, інбокс slicer-uploads |
| `/projects/*` | Групування за проєктами | CRUD, план друку, архіви проєкту |
| `/macros/*` | G-code- та MQTT-action-макроси | CRUD, виконання |
| `/notifications/*`, `/notification-templates/*`, `/user-notifications/*`, `/telegram/*` | Вихідні канали | CRUD провайдерів, перевизначення шаблонів, тестове надсилання, конфіг Telegram-бота |
| `/spoolman/*`, `/inventory/*` | Облік котушок | синхронізація, мапінг слотів, каталог кольорів/котушок |
| `/smart-plugs/*` | Конфіг розумних розеток | CRUD, енергетичні snapshot-и, ручне on/off |
| `/system/*`, `/support/*`, `/updates/*`, `/firmware/*` | Здоров'я + діагностика + оновлення | `health`, налаштування, бекап, відновлення, debug-bundle, перевірка прошивки |
| `/local-backup/*`, `/git-backup/*` | Провайдери бекапів | запуск, відновлення, розклад |
| `/maintenance/*`, `/kprofiles/*` | Облік сервісного обслуговування + K-профілі | журнал, заплановане, CRUD |
| `/external-links/*`, `/ams-history/*` | Різні UX | посилання дашборду, історія зміни слотів AMS |
| `/metrics`, `/webhook/*`, `/obico/*`, `/virtual-printers/*` | Інтеграції | метрики Prometheus, вхідні webhook-и, Obico AI, віртуальний принтер (ціль для slicer-а) |

Повний перелік є в `/docs` — ця таблиця лише вказує, де шукати.

---

## :material-camera-iris: Потоки камери та бінарні endpoint-и

Деякі endpoint-и не можуть приймати заголовок `Authorization`, бо їх споживають теги `<img>` / `<video>`. Вони використовують короткоживучий **stream-токен** (TTL 60 хв), що передається як query-параметр.

```bash
# 1. Mint a token (auth required)
TOKEN=$(curl -s -H "X-API-Key: bb_..." \
  -X POST https://bamdude.example.com/api/v1/printers/camera/stream-token \
  | jq -r .token)

# 2. Use it on binary endpoints
curl "https://bamdude.example.com/api/v1/printers/2/camera/snapshot?token=$TOKEN" -o snap.jpg
```

Endpoint-и за stream-токен-шлюзом:

| Endpoint | Повертає |
|----------|----------|
| `GET /printers/{id}/camera/stream?token=...` | потік MJPEG |
| `GET /printers/{id}/camera/snapshot?token=...` | JPEG-знімок |
| `GET /printers/{id}/cover?token=...` | мініатюра обкладинки поточного друку (видається з локального архіву — ніколи не ініціює FTP-вибірку) |
| `GET /printers/{id}/plate-detection-reference?token=...` | референсне зображення для детекції очищеного столу |
| `GET /obico/cached-frame/{nonce}` | URL кадру, який передається в ML-API Obico. Стоїть у білому списку auth-middleware, бо GET від Obico не може нести bearer-заголовок — самим капабіліті є nonce. |

Веб-інтерфейс кешує stream-токен у межах сесії та оновлює його перед закінченням терміну.

---

## :material-bell-ring: Webhook-и та події в реальному часі

BamDude **не** надає вихідних webhook-ів для подій застосунку. Використовуйте [провайдер сповіщень](../features/notifications.md) (Telegram, Discord, ntfy, Pushover, Email, Home Assistant), коли потрібен односторонній push.

Канал реального часу — це WebSocket за адресою `wss://<host>/api/v1/ws`. Він несе:

- оновлення статусу принтерів (температури, прогрес, стан AMS)
- прогрес dispatch-у та черги
- події створення / оновлення архівів
- стан розумних розеток та тики споживання енергії

Автентифікуйте WebSocket так само, як REST — access-токен приймається через заголовок `Authorization` під час handshake-у апгрейду.

---

## :material-script-text-play: Поширені операції — швидкі рецепти

### Список останніх 50 архівів принтера

```bash
curl -H "X-API-Key: bb_..." \
  "https://bamdude.example.com/api/v1/archives/?printer_id=2&page=1&per_page=50"
```

### Додати файл бібліотеки в чергу (3 копії, фіксований мапінг AMS)

```bash
curl -X POST \
  -H "X-API-Key: bb_..." \
  -H "Content-Type: application/json" \
  -d '{
    "library_file_id": 42,
    "queue_id": 2,
    "ams_mapping": [0, 1, 2, 3],
    "quantity": 3
  }' \
  "https://bamdude.example.com/api/v1/queue/"
```

### Отримати поточний статус принтера

```bash
curl -H "X-API-Key: bb_..." \
  "https://bamdude.example.com/api/v1/printers/2/status"
```

### Пропустити окремі об'єкти посеред друку

Тіло запиту — JSON-масив ID об'єктів, що їх повідомив slicer.

```bash
curl -X POST \
  -H "X-API-Key: bb_..." \
  -H "Content-Type: application/json" \
  -d '[100, 200]' \
  "https://bamdude.example.com/api/v1/printers/2/print/skip-objects"
```

### Відновити втрачений 3MF для архіву

Коли `on_print_start` не зміг забрати 3MF через FTP (принтер недосяжний, FTP-таймаут), рядок архіву створюється з `extra_data.no_3mf_available = true`. Фонові обходи повторюють спробу автоматично; ви також можете запустити це вручну:

```bash
curl -X POST \
  -H "X-API-Key: bb_..." \
  "https://bamdude.example.com/api/v1/archives/123/retry-download"
```

### Запустити очищення 3MF архівів (попередній перегляд, потім запуск)

Завдання очищення видаляє бінарники 3MF для архівів, старших за вікно зберігання (рядок з метаданими залишається). Денний cron виконується автоматично; для разового обходу:

```bash
# Dry-run preview — what would be deleted, total bytes
curl -H "X-API-Key: bb_..." \
  "https://bamdude.example.com/api/v1/archives/cleanup/preview"

# Run it
curl -X POST -H "X-API-Key: bb_..." \
  "https://bamdude.example.com/api/v1/archives/cleanup/run"

# Inspect the daily cron's last run + next run
curl -H "X-API-Key: bb_..." \
  "https://bamdude.example.com/api/v1/archives/cleanup/status"
```

### Оновити JWT-сесію зі свого клієнта

```bash
# /auth/refresh reads the HttpOnly bamdude_refresh cookie set during login.
# --cookie-jar / --cookie persists it across calls.
curl -c cookies.txt -X POST \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "..."}' \
  https://bamdude.example.com/api/v1/auth/login

curl -b cookies.txt -c cookies.txt -X POST \
  https://bamdude.example.com/api/v1/auth/refresh
```

Відповідь refresh повертає новий access-токен і ротує refresh-cookie на місці. Повторне використання вже використаного refresh-токена анулює всю сім'ю токенів на всіх пристроях (детекція повторного використання за OWASP).

---

## :material-code-json: Версіонування та стабільність

- Версійний префікс API — `/api/v1`. Зміни, що ламають сумісність, поставлятимуться під `/api/v2`, а не мутацією v1.
- Адитивні зміни (нові endpoint-и, нові опціональні поля) приходять у patch- / minor-релізах без попередження. Якщо ви залежите від стабільності форми відповіді, фіксуйте версію контейнера BamDude.
- Депрекації оголошуються в [changelog](../about/changelog.md) щонайменше за один minor-реліз до видалення.

> Початково базується на [Bambuddy](https://github.com/maziggy/bambuddy).
