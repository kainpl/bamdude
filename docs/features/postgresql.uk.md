---
title: Підтримка PostgreSQL
description: Опціональний PostgreSQL backend для великих ферм принтерів
---

# Підтримка PostgreSQL

BamDude підтримує опціональний PostgreSQL backend для користувачів, яким потрібна краща конкурентність, реплікація або інтеграція з існуючою інфраструктурою. SQLite залишається за замовчуванням — додаткова конфігурація не потрібна.

---

## :material-database: Коли використовувати PostgreSQL

| Сценарій | Рекомендація |
|----------|:-----------:|
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

---

## :material-swap-horizontal: Міграція з SQLite на PostgreSQL

### Автоматична міграція

При першому переході на PostgreSQL:

1. Встановіть `DATABASE_URL`
2. Перезапустіть BamDude
3. Система виявляє порожній PostgreSQL + локальний `bamdude.db`
4. **Автоматично переносить всі дані** з SQLite в PostgreSQL
5. Перейменовує `bamdude.db` → `bamdude.db.migrated`

!!! info "Ручні кроки не потрібні"
    Міграція повністю автоматична. Всі таблиці, налаштування, архіви, котушки, черги та облікові записи переносяться.

---

## :material-backup-restore: Резервне копіювання

### Портативний формат

Бекапи **завжди в SQLite форматі** незалежно від backend. Це забезпечує:

- Бекапи з PostgreSQL можна відновити на SQLite (і навпаки)
- Бекапи — один файл, портативний
- Не залежить від `pg_dump`

### Відновлення на PostgreSQL

При відновленні SQLite бекапу на PostgreSQL інсталяції — дані імпортуються автоматично з конвертацією типів (boolean, datetime).

---

## :material-alert: Обмеження

!!! warning "Створіть базу даних заздалегідь"
    BamDude **не створює** PostgreSQL базу даних — вона має вже існувати. Тільки таблиці створюються автоматично.

!!! tip "Повернення на SQLite"
    Щоб повернутись на SQLite: видаліть `DATABASE_URL`, перезапустіть. Ваш `bamdude.db.migrated` файл містить оригінальні дані — перейменуйте назад в `bamdude.db`.

> Базується на документації [Bambuddy](https://github.com/maziggy/bambuddy).
