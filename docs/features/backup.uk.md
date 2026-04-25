---
title: Резервне копіювання та відновлення
description: Ручні ZIP-бекапи, заплановані локальні бекапи та Git-пуш профілів принтерів
---

# Резервне копіювання та відновлення

Три незалежні шляхи захищають вашу інсталяцію: on-demand ZIP з UI, scheduled job на локальний диск, що тримає N останніх знімків, і Git-пуш, що архівує профілі принтерів у GitHub або GitLab.

---

## :material-backup-restore: Що всередині Backup ZIP

On-demand і scheduled локальні бекапи продукують ту саму структуру ZIP. Записи верхнього рівня:

| Запис | Вміст |
|-------|-------|
| `bamdude.db` | Повна база даних, **завжди експортується як portable SQLite** — навіть коли runtime у вас PostgreSQL, дамп проходить через `dump_to_sqlite()`, тож той самий ZIP відновлюється на будь-який backend. |
| `archive/` | Кожна директорія per-print архіву: `.3mf`, мініатюра PNG, plate-N.png і per-archive folder. |
| `virtual_printer/` | Pending-аплоди + working state віртуального принтера. |
| `plate_calibration/` | Reference-кадри + ROI-визначення для plate detection. |
| `icons/` | Кастомні іконки, завантажені для принтерів / проєктів. |
| `projects/` | Вкладення проєктів. |

Виключено за дизайном: `logs/`, кеші, тимчасові файли, bundled frontend (він шипиться в образі / репі). Деякі sensitive поля також фільтруються перед дампом БД — LDAP bind password ніколи не повертається в API responses, а API-ключі зберігаються як one-way хеші.

!!! note "PostgreSQL → SQLite → PostgreSQL"
    Навіть на PostgreSQL runtime `dump_to_sqlite()` нормалізує експорт. Відновлення на свіжій PostgreSQL інсталяції запускає зворотний `import_sqlite_to_postgres()` і пере-створює рядки в живій БД. Той самий ZIP також відновлюється на SQLite-інсталяцію без жодних додаткових кроків.

---

## :material-download: Ручний бекап

1. **Налаштування → Система → Backup & Restore**
2. Натисніть **Create Backup**
3. Браузер качає `bamdude-backup-YYYYMMDD-HHMMSS.zip`

ZIP стримиться з тимчасового файлу, а не буферизується в пам'яті, тож multi-gigabyte бекапи не OOM-ять процес. Тимчасовий файл видаляється автоматично, як тільки response завершується.

API: `GET /api/v1/settings/backup` (потрібно `settings:backup`).

---

## :material-clock-outline: Заплановані локальні бекапи

Налаштовуються в **Налаштування → Система → Local Backup Schedule**. Шедулер тікає раз на хвилину і запускає due-jobs у той самий ZIP-builder, що використовує ручна кнопка, потім обрізає старіші бекапи понад retention limit.

| Налаштування | За замовчуванням | Примітки |
|--------------|------------------|----------|
| `local_backup_enabled` | `false` | Master switch. |
| `local_backup_schedule` | `daily` | `hourly`, `daily` або `weekly`. |
| `local_backup_time` | `03:00` | `HH:MM` для daily/weekly запусків (server-local time). Hourly це поле ігнорує. |
| `local_backup_retention` | `5` | Тримати N останніх бекапів; старіші автоматично обрізаються. Діапазон 1–100. |
| `local_backup_path` | порожньо | Output-директорія. Порожньо = `data/backups/`. |

Сторінка налаштувань показує last-run timestamp + outcome (`success` / `failed`), наступний запланований запуск і список наявних retention-бекапів з розмірами файлів. Ручні запуски "Create Backup" зберігаються в тій самій директорії і враховуються в retention.

Legacy `bambuddy-backup-*.zip` файли (від upstream-інсталяцій) досі лістяться і піддаються відновленню, тож апгрейд не залишає попередні знімки сиротами.

---

## :material-source-branch: Git-бекап (профілі в GitHub / GitLab)

Окремий від ZIP-флоу. **Налаштування → Система → Git Backup** пушить вибрані дані профілів принтерів у GitHub або GitLab репозиторій — корисно для off-site синку профілів, координації multi-host ферми і PR-based історії змін у налаштуваннях принтера.

### :material-cog-outline: Конфігурація

| Налаштування | Примітки |
|--------------|----------|
| Provider | `github` або `gitlab`. |
| Repository URL | Повний clone URL (HTTPS-форма). |
| Access Token | Personal Access Token. Зберігається зашифрованим at rest. |
| Гілка | Цільова гілка (за замовчуванням `main`). |
| API base URL | Тільки для self-hosted GitLab. |
| Schedule | `hourly` / `daily` / `weekly` або off. |

### :material-checkbox-marked: Що пушиться

Перемикається незалежно:

- **K-profiles** — per-printer K-profile JSON.
- **Cloud profiles** — Bambu Cloud filament profiles per user.
- **Settings** — таблиця application settings (sensitive поля виключено).
- **Spools** — повний дамп інвентаря.
- **Archives** — записи історії друку.

Тільки змінені файли генерують комміти — no-op запуск пишеться як `skipped`.

### :material-monitor-dashboard: Панель статусу

Сторінка налаштувань показує live-статус:

- **Last backup** — timestamp, статус (`success` / `failed` / `skipped`), commit SHA і повідомлення.
- **Next scheduled run** — коли шедулер фаєрне далі.
- **Log table** — історичні запуски з тригером (`manual` / `scheduled`), тривалістю і будь-яким error message.
- **Run Now** — кнопка миттєвого пушу незалежно від розкладу.

Frequency пушу, content-чекбокси і креденшали редагуються наживо без рестарту BamDude.

---

## :material-upload: Відновлення з Backup ZIP

1. **Зупиніть BamDude** перед відновленням (інакше upload нижче замінить файли під запущеним процесом — ризиково).
2. Або киньте ZIP у data-директорію і дайте BamDude задетектити його при наступному boot, або скористайтесь **Налаштування → Система → Restore** і завантажте через форму.
3. На boot / submit форми BamDude:
   - Розпаковує ZIP у temp-dir
   - Закриває поточні DB-конекшени
   - Замінює базу (`bamdude.db` import на SQLite, `import_sqlite_to_postgres` на PG)
   - Замінює `archive/`, `virtual_printer/`, `plate_calibration/`, `icons/`, `projects/`
   - Re-ініціалізує базу (запускає pending міграції на відновлених даних)
   - Видаляє source ZIP після успіху

!!! danger "Restore замінює поточний стан"
    Restore перезаписує живу БД і перелічені вище data-директорії. **Зробіть свіжий бекап поточного стану перед цим**, якщо може знадобитися відкатити сам restore.

API: `POST /api/v1/settings/restore` (multipart `file=…`, потрібно `settings:restore`).

### :material-database-arrow-right: Cross-backend restore

Portable SQLite-дамп означає, що ви можете:

- Зробити бекап з **SQLite** інсталяції → відновити на **PostgreSQL** (loader мігрує рядки).
- Зробити бекап з **PostgreSQL** інсталяції → відновити на **SQLite** (БД уже експортовано як SQLite).
- Зробити бекап з PG → відновити на свіжий PG (loader реімпортує SQLite у PG).

Конфліктні primary keys мерджаться або скіпаються per-row залежно від таблиці — referential integrity зберігається через міграцію.

---

## :material-lightbulb: Поради

!!! tip "Off-site покриття"
    Скомбінуйте **Заплановані локальні бекапи** (повні дані, on-disk) з **Git-бекапом** (профілі, off-site) — локальний переживе software wipe, git — переживе hardware loss.

!!! tip "Бекап перед оновленням"
    [`UPDATING.md`](https://github.com/kainpl/bamdude/blob/main/UPDATING.md) рекомендує свіжий ручний бекап перед кожним minor-апгрейдом. Міграції ідемпотентні і one-shot, але автоматичного шляху для downgrade немає.

> Початково базується на документації [Bambuddy](https://github.com/maziggy/bambuddy).
