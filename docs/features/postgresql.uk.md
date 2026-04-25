---
title: Підтримка PostgreSQL
description: Опціональний бекенд PostgreSQL для великих ферм принтерів
---

# Підтримка PostgreSQL

BamDude підтримує опціональний бекенд PostgreSQL для користувачів, яким потрібна краща конкурентність, реплікація або інтеграція з існуючою інфраструктурою. SQLite залишається за замовчуванням — додаткова конфігурація не потрібна.

---

## :material-database: Коли використовувати PostgreSQL

| Сценарій | Рекомендація |
|----------|:------------:|
| Один користувач, 1-5 принтерів | SQLite |
| Мала ферма, < 10 принтерів | SQLite |
| Велика ферма, 10+ принтерів | PostgreSQL |
| Висока конкурентність (багато API клієнтів) | PostgreSQL |
| Потрібна реплікація/бекап БД | PostgreSQL |
| Існуюча інфраструктура PostgreSQL | PostgreSQL |
| Простий запуск, без додаткових сервісів | SQLite |

---

## :material-cog: Конфігурація

### Змінна середовища

Встановіть `DATABASE_URL` для переходу з SQLite на PostgreSQL:

```bash
DATABASE_URL=postgresql+asyncpg://bamdude:password@localhost:5432/bamdude
```

| Компонент | Значення |
|-----------|----------|
| Driver | `postgresql+asyncpg` (обов'язково) |
| User | Користувач БД |
| Password | Пароль БД |
| Host | Адреса PostgreSQL сервера |
| Port | За замовчуванням `5432` |
| Database | Має вже існувати |

### Docker Compose

```yaml
services:
  bamdude:
    image: ghcr.io/kainpl/bamdude:latest
    network_mode: host
    environment:
      - TZ=Europe/Kyiv
      - DATABASE_URL=postgresql+asyncpg://bamdude:password@localhost:5432/bamdude
    volumes:
      - bamdude_data:/app/data
      - bamdude_logs:/app/logs
    restart: unless-stopped

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: bamdude
      POSTGRES_USER: bamdude
      POSTGRES_PASSWORD: password
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    restart: unless-stopped

volumes:
  bamdude_data:
  bamdude_logs:
  postgres_data:
```

### Файл .env

```env
DATABASE_URL=postgresql+asyncpg://bamdude:password@postgres:5432/bamdude
```

---

## :material-swap-horizontal: Міграція з SQLite на PostgreSQL

### Автоматична міграція

При першому переході на PostgreSQL:

1. Встановіть `DATABASE_URL` у вашому середовищі
2. Перезапустіть BamDude
3. BamDude виявляє:
    - PostgreSQL порожня (свіжа база даних)
    - Локальний `bamdude.db` існує
4. **Автоматично переносить усі дані** з SQLite до PostgreSQL
5. Перейменовує `bamdude.db` → `bamdude.db.migrated`

!!! info "Ручні кроки не потрібні"
    Міграція повністю автоматична. Усі таблиці, налаштування, архіви, котушки, черги та облікові записи переносяться.

### Що мігрує

- Усі таблиці й дані (принтери, архіви, котушки, налаштування, користувачі тощо)
- Конвертація типів: SQLite boolean (0/1) → PostgreSQL boolean, datetime-рядки → timestamps
- Послідовності (sequences) скидаються до коректних значень (auto-increment ID)
- Повнотекстовий пошуковий індекс перебудовується на PostgreSQL tsvector + GIN

### Що НЕ мігрує

- FTS5 віртуальні таблиці (замінюються на PostgreSQL tsvector)
- WAL/SHM файли (специфічні для SQLite)
- Таблиця `_migrations` (створюється з нуля)

---

## :material-backup-restore: Резервне копіювання та відновлення

### Портативний формат бекапу

Бекапи **завжди в SQLite форматі** незалежно від бекенду БД. Це забезпечує:

- Бекапи з PostgreSQL можна відновити на SQLite (і навпаки)
- Бекапи — один файл, портативні та зручні для перегляду
- Без залежності від `pg_dump` чи інших інструментів

### Резервне копіювання (PostgreSQL → SQLite ZIP)

1. Перейдіть до **Налаштування** > **Резервне копіювання**
2. Натисніть **Створити бекап**
3. BamDude експортує всі таблиці PostgreSQL до тимчасового файлу SQLite
4. Пакує його разом з архівами, іконками та іншими каталогами даних у ZIP

### Відновлення (SQLite ZIP → PostgreSQL)

1. Перейдіть до **Налаштування** > **Резервне копіювання**
2. Завантажте ZIP бекапу
3. BamDude імпортує дані SQLite до PostgreSQL з автоматичною конвертацією типів

---

## :material-magnify: Повнотекстовий пошук

BamDude використовує різні реалізації повнотекстового пошуку залежно від бази даних:

| Можливість | SQLite | PostgreSQL |
|------------|--------|------------|
| Engine | FTS5 віртуальна таблиця | tsvector + GIN-індекс |
| Синтаксис запитів | `MATCH` із wildcard | `to_tsquery` із префіксним матчингом |
| Тригери | Тригери INSERT/UPDATE/DELETE | Функція BEFORE INSERT OR UPDATE |
| Перебудова | Видалення + повторний INSERT FTS-рядків | Тригер UPDATE спрацьовує повторно |
| Ваги | Без ваг | A (name) > B (filename, tags) > C (designer, filament) > D (notes) |

Обидві реалізації прозорі для користувача — пошуковий API працює однаково.

---

## :material-connection: Пул з'єднань

| Параметр | SQLite | PostgreSQL |
|----------|--------|------------|
| Розмір пулу | 20 | 10 |
| Max overflow | 200 | 20 |
| Busy timeout | 15s (PRAGMA) | На рівні з'єднання |

---

## :material-clock-alert: Повільні міграції при першому запуску

Деякі міграції за своєю природою повільні незалежно від бекенду БД, оскільки вузьким місцем є відкриття 3MF-файлів на диску, а не запис до БД:

- **m022** (0.4.1) читає один config-файл усередині кожного існуючого 3MF, щоб заповнити нові прапорці `gcode_label_objects` + `exclude_object`. Приблизно 50-200 мс на файл. Інсталяція з тисячами архівів може провести кілька хвилин на кроці міграції перед тим, як підніметься API. На PostgreSQL такий самий час, як і на SQLite.

Міграція логує прогрес кожні 100 рядків — слідкуйте за рядками `m022 library_files: progress` та `m022 print_archives: progress`, якщо здається, що завантаження зависло. Файли, які видалені з диска, мовчки пропускаються.

---

## :material-alert: Обмеження

!!! warning "Створіть базу даних заздалегідь"
    BamDude **не створює** базу даних PostgreSQL — вона має вже існувати. Тільки таблиці створюються автоматично.

!!! warning "Без pg_dump через UI"
    Бекап через веб-інтерфейс завжди експортує у форматі SQLite. Для нативних PostgreSQL-бекапів використовуйте `pg_dump` напряму.

!!! tip "Повернення на SQLite"
    Щоб повернутись на SQLite: видаліть `DATABASE_URL`, перезапустіть. Ваш файл `bamdude.db.migrated` досі містить оригінальні дані SQLite — перейменуйте назад в `bamdude.db`.

---

## :material-lightbulb: Поради

!!! tip "Тестування з Docker Compose"
    Використайте приклад Docker Compose вище, щоб локально протестувати PostgreSQL перед розгортанням у продакшн.

!!! tip "Безпека рядка з'єднання"
    Уникайте використання паролів у змінних середовища у продакшні. Використовуйте Docker secrets або файл `.env` з обмеженими правами доступу.
