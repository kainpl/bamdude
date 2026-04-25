---
title: Оновлення та міграція
description: Backup-first протокол оновлення BamDude, повна мапа міграцій для 0.4.0 / 0.4.1 та процедура відкату.
---

# Оновлення та міграція

Цей посібник -- оператор-протокол безпечного оновлення. Схема БД рухається **тільки вперед** -- автоматичної down-міграції немає. Якщо треба повернутись назад, відновлюйтесь із резервної копії.

> **Завжди робіть backup `data/` (або тома `bamdude_data` у Docker) перед будь-яким оновленням.** А саме: `bamdude.db` (або dump PostgreSQL, якщо у вас PG), директорія `archive/` (3MF + мініатюри) та директорія `library/`.

---

## :material-clipboard-check: 1. Чек-ліст перед оновленням

Перед тим, як щось чіпати:

1. **Зупиніть сервіс BamDude** (`sudo systemctl stop bamdude` або `docker compose down`).
2. **Зробіть backup директорії з даними** -- див. команди backup нижче.
3. **Запишіть свою поточну версію** -- відкрийте `/system/health` у браузері або виконайте `cat pyproject.toml | grep version` для нативних інсталяцій. Корисно, якщо доведеться відкочуватись.
4. **Якщо ви за reverse proxy** (nginx / Caddy / Traefik), скопіюйте конфіг убік, щоб перевірити його після оновлення.
5. **Перевірте розмір логів** -- якщо `data/logs/` величезна, це гарний момент її зротейтити.

### Команди backup

=== "Docker volumes"

    ```bash
    # Том даних -- sqlite БД, архіви, мініатюри, аплоади
    docker run --rm \
      -v bamdude_data:/from \
      -v "$(pwd)/backup":/to \
      alpine tar czf /to/bamdude-data-$(date +%Y%m%d).tar.gz -C /from .

    # Логи (опційно)
    docker run --rm \
      -v bamdude_logs:/from \
      -v "$(pwd)/backup":/to \
      alpine tar czf /to/bamdude-logs-$(date +%Y%m%d).tar.gz -C /from .
    ```

=== "Native -- UI backup (рекомендовано)"

    Відкрийте **Налаштування → Backup → Локальна резервна копія → Створити резервну копію**, потім **Завантажити резервну копію**, щоб зберегти zip собі на комп. Zip пакує SQLite БД, директорію архіву, мініатюри, аплоади та конфіг у тому самому layout-і, який `install.sh` розкладає на диску -- відновлення -- це просто "розпакувати в шлях інсталяції та перезапустити". Він також захоплює метадані ключа шифрування та стан запланованих бекапів, які `tar` сирого `data/` лишає позаду.

=== "Native -- shell"

    ```bash
    cd /opt/bamdude
    tar czf ~/bamdude-data-$(date +%Y%m%d).tar.gz data/
    ```

=== "PostgreSQL"

    ```bash
    pg_dump -Fc -f ~/bamdude-$(date +%Y%m%d).dump "$DATABASE_URL"
    # Плюс затарте директорії archive/ + library/ з тома даних.
    ```

---

## :material-docker: 2. Процедура оновлення -- Docker

```bash
cd bamdude
docker compose pull          # якщо запіноване на :latest
docker compose up -d
docker compose logs -f       # дивитись, як застосовуються міграції
```

Пінити конкретний тег у `compose.yaml` -- це ок і навіть рекомендовано для стабільних інсталяцій -- `:0.4.1` ніколи не зрушить; `:latest` йде за `main`.

```yaml
# Запіноване, рекомендовано
image: ghcr.io/kainpl/bamdude:0.4.1

# Rolling, йде за main
image: ghcr.io/kainpl/bamdude:latest
```

Слідкуйте за стартовим логом для прогресу міграцій. Довгі міграції логують пакетний прогрес (напр. `m020 library_files: progress` на m020/m022). **Чекайте на "Migrations complete", перш ніж тестувати.**

```text
INFO  [backend.app.migrations] Applying m019_archive_queue_batch_error
INFO  [backend.app.migrations] Applied m019 (version 19)
INFO  [backend.app.migrations] Applying m022_label_object_metadata_backfill
INFO  [backend.app.migrations] m022 library_files: progress 100/847
INFO  [backend.app.migrations] m022 library_files: progress 200/847
...
INFO  [backend.app.migrations] Applied m022 (version 22)
INFO  [backend.app.main] Startup complete
```

Sanity-check `/system/health` повертає 200.

---

## :material-server: 3. Процедура оновлення -- Manual (Python venv)

```bash
cd /opt/bamdude
sudo systemctl stop bamdude

# Підтягнути сорси
sudo -u bamdude git fetch
sudo -u bamdude git checkout v0.4.1     # або який там тег

# Python-залежності
sudo -u bamdude ./venv/bin/pip install -r requirements.txt --upgrade

# Frontend-бандл (регенерує треканий каталог static/).
# Пропустіть цей крок, якщо завжди тягнете pre-built теги -- бандл їде
# in-tree на release-коміті. Потрібно лише якщо збираєте з кастомної гілки.
sudo -u bamdude bash -c 'cd frontend && npm ci && npm run build'

# Перезапустити та хвостити логи
sudo systemctl start bamdude
sudo journalctl -u bamdude -f
```

Поставлений `install/update.sh` автоматизує всю послідовність (stop → backup → git pull → pip install → npm build → start) і підтримує env-перевизначення:

```bash
sudo /opt/bamdude/install/update.sh
```

| Змінна | Дефолт | Призначення |
|---|---|---|
| `INSTALL_DIR` | `/opt/bamdude` | Де живе BamDude |
| `SERVICE_NAME` | `bamdude` | systemd-юніт для рестарту |
| `BRANCH` | поточна вичекаутена гілка | Перемкнутись на іншу гілку під час оновлення |
| `BACKUP_MODE` | `auto` | `auto` пропускає, коли нема чого бекапити, `require` обриває, якщо backup впав, `skip` вимикає |
| `FORCE` | `0` | Виставити `1`, щоб обійти перевірки dirty-worktree / backup |

---

## :material-database-cog: 4. Огляд міграцій -- що міняє кожна версія

BamDude трекає застосовані міграції в таблиці `_migrations`. Кожен реліз запускає всі очікуючі версії по порядку при першому завантаженні. Нові інсталяції спочатку запускають `create_all()` (створюючи таблиці з поточних означень моделей), потім `m000` + `m001` пре-проштамповуються як застосовані через bootstrap-крок, і фактично виконуються тільки пізніші міграції.

Міграції, помічені **seed**, містять DML-крок (бекфіл / нормалізація даних) і можуть займати помітний час на великих інсталяціях; чисто-DDL міграції (додавання колонок, swap FK) завершуються за мілісекунди.

| Версія | Назва | Що змінює | Seed | Вперше потрібна в |
|---|---|---|---|---|
| **m000** | `bambuddy_to_bamdude_301` | Імпортує спадковий `bambuddy.db` / `bambutrack.db`, якщо знайдений поруч із тим, де BamDude очікує знайти `bamdude.db`. No-op, коли спадкової БД нема. Оригінальний файл Bambuddy **перейменовується**, не видаляється, тож відкат можливий. | yes (import) | Форки/оновлення з Bambuddy 2.2.2 |
| **m001** | `bamdude_baseline` | Створює FTS-індекс для пошуку по архівах (FTS5 на SQLite, tsvector + GIN на PostgreSQL) та засіює початкові довідкові дані (каталог моделей принтерів, дефолтні групи тощо). | yes | Свіжі інсталяції BamDude |
| **m002** | `bamdude_311` | Bump схеми BamDude 3.0.1 → 3.1.1. Додає `printer_queues`, `macros`, колонки swap mode, конфіг stagger, таблиці історії обслуговування, переробку черги (`queue_id`), `printer_models` на типах обслуговування. Дропає мертву таблицю `filaments`. | yes | Оновлення з BamDude 3.0.x |
| **m003** | `enforce_admin_user` | Кодифікує always-on модель авторизації: штампує `auth_enabled=true` + `setup_completed=true`, якщо існує хоча б один admin; інакше чистить обидва прапорці, щоб наступне завантаження направило користувача через `/setup`. Схема не змінюється. | yes | Усі інсталяції |
| **m004** | `m002_reconcile` | Перезапускає `m002.upgrade()` дослівно. Ловить інсталяції, що застрягли на ранній версії m002 (до правила frozen-migrations), де версія була позначена як застосована, але пізніші правки m002 ніколи не виконались. | yes | Застряглі post-3.1.1 інсталяції |
| **m005** | `swap_profiles` | Друга вимірність на swap mode: `printers.swap_profile` + `macros.swap_profile`. Перепривʼязує наявні вбудовані макроси A1 Mini до `swap_profile='a1mini_kit'`; засіває порожні вбудовані для `a1mini_stl` + `jobox-a1`. | yes | Усі інсталяції |
| **m006** | `mesh_mode_fast_check` | Додає `print_queue.mesh_mode_fast_check BOOLEAN DEFAULT 1`, щоб оператор міг відмовитись від bed-mesh fast-check probe для кожного елемента черги. | no | Усі інсталяції |
| **m007** | `drop_vibration_cali` | Дропає `print_queue.vibration_cali` (Bambu Studio тепер хардкодить це у `false` для кожної моделі; живе тільки в калібрувальному візарді). MQTT-payload усе ще емітить ключ для сумісності з прошивкою. | no | Усі інсталяції |
| **m008** | `swap_macro_queue_fields` | Додає `print_queue.execute_swap_macros BOOLEAN DEFAULT 1` + `swap_macro_events TEXT (JSON array)`, щоб кожен елемент черги міг перевизначити, які swap-події для нього стріляють. | no | Усі інсталяції |
| **m009** | `archive_source_hash` | Додає `print_archives.source_content_hash` (SHA256 непатченого джерела) + `applied_patches` (JSON). Запити дедупу перемикаються на `COALESCE(source_content_hash, content_hash)`, щоб BamDude-патчені архіви дедупились проти оригіналів з бібліотеки. | no | Усі інсталяції |
| **m010** | `queue_reliability` | Додає `print_archives.subtask_id VARCHAR(64)` (advisory archive matching через перезапуски) + `printers.awaiting_plate_clear BOOLEAN DEFAULT 0` (персистоване plate-clear ворота, переживає Auto Off power-cycle). | no | Усі інсталяції |
| **m011** | `cloud_region` | Додає `users.cloud_region VARCHAR(10)`, щоб per-user облікові дані Bambu Cloud несли свій регіон. Закриває cross-tenant витік регіону, який мав singleton-сервіс. | no | Усі інсталяції |
| **m012** | `mfa` | Кластер MFA / 2FA / OIDC -- шість нових таблиць: `user_totp`, `user_otp_codes`, `auth_ephemeral_tokens`, `auth_rate_limit_events`, `oidc_providers`, `user_oidc_links`, плюс `users.password_changed_at`. Підтримує always-on модель авторизації з 0.4.0. | no | 0.4.0 |
| **m013** | `library_file_print_count` | Додає `library_files.print_count INTEGER DEFAULT 0`. Per-file лічильник завершених друків, інкрементується в `on_print_complete`. | no | 0.4.0 |
| **m014** | `archive_library_link` | Додає `print_archives.library_file_id` FK (`ON DELETE SET NULL`) + бекфілить його на кожному наявному архіві шляхом hash-метчингу проти `library_files.file_hash`. **Перераховує `library_files.print_count` та `last_printed_at` з історії завершених архівів** (перезаписує попередні значення -- історія архівів є авторитативною). | yes | 0.4.0 |
| **m015** | `refresh_token_support` | Додає `auth_ephemeral_tokens.used_at` + `family_id` для підтримки sliding-session refresh-флоу (§18.14). Reuse-detection відкликає всю фемілі, якщо refresh-токен реплейнуть. | no | 0.4.0 |
| **m016** | `project_print_plan` | Створює `project_print_plan_items` (per-project упорядкований список `.3mf`-файлів зі степером копій). Бекфілить один рядок на кожен наявний linked `library_files.project_id` з copies=1. | yes | 0.4.0 |
| **m017** | `macro_action_type` | Додає `macros.action_type` + `mqtt_action` + `delay_seconds`. Дозволяє макросу інвокувати MQTT-команду (`chamber_light_off`, `chamber_light_on`) замість gcode на події `print_started` / `print_finished` з опційною затримкою. | no | 0.4.0 |
| **m018** | `queue_library_fk_set_null` | Змінює FK `print_queue.library_file_id` з `ON DELETE CASCADE` на `ON DELETE SET NULL`. У комбінації з in-app каскадом у `delete_file` це дає SQLite ту саму поведінку, що PostgreSQL отримує нативно. | no | 0.4.0 |
| **m019** | `archive_queue_batch_error` | Рефакторинг queue↔archive. Додає `print_archives.queue_id` (FK, indexed) + `batch_id` (VARCHAR(36), indexed) + `error_message` (TEXT). Дропає чотири кешовані лічильники з `printer_queues` (`completed_count` / `failed_count` / `cancelled_count` / `total_count`). Бекфілить `queue_id`/`batch_id`/`error_message` з наявних посилань `print_queue.archive_id`. **Видаляє завершені елементи черги, що мають архівне посилання** -- бекфіл-еквівалент нового авточищення в `on_print_complete`. | yes | 0.4.0 |
| **m020** | `spool_purchase_date` | Додає три колонки в `spool`: `purchase_date DATETIME`, `filament_diameter VARCHAR(8) NOT NULL DEFAULT '1.75'`, `lot INTEGER`. Бекфілить `filament_diameter` у `'1.75'` (дефолт Bambu). | yes | 0.4.0 (post-b2) |
| **m021** | `drop_auto_light_off` | Дропає спадкову колонку `printers.auto_light_off`. Замінено фреймворком макросів (сконфігуруйте mqtt-action макрос `chamber_light_off` на події `print_started` для того самого ефекту, плюс опційний симетричний `chamber_light_on` на `print_finished`). | no | 0.4.0 |
| **m022** | `label_object_metadata_backfill` | Відкриває кожен наявний на диску 3MF, витягає `gcode_label_objects` + `exclude_object` з `Metadata/project_settings.config`, вмерджує їх у `library_files.file_metadata` та `print_archives.extra_data`. **Довгий старт на першому завантаженні, якщо у вас багато архівів** -- див. [§5 Помітні шляхи оновлення](#5-pomitni-shlyakhy-onovlennya). | yes | 0.4.1 |

---

## :material-arrow-decision: 5. Помітні шляхи оновлення

### З Bambuddy 3.0.x → BamDude 0.4.x

`m000` імпортує ваші дані, `m002` адаптує схему, `m005`+ -- BamDude-нативні.

!!! warning "Завжди оновлюйтесь до **0.4.0.1** або пізніше"
    Перехід зі спадкової інсталяції 3.0.1 одразу на **0.4.0** падав на `m005_swap_profiles.seed()` з `no such column: printers.awaiting_plate_clear` -- seed використовував ORM-овий `select(Printer)`, який підвантажував кожну колонку з *поточної* моделі, включно з колонками, яких на момент m005 у ланцюжку ще не існує. Виправлено в 0.4.0.1 переписуванням seed на raw SQL з явними списками колонок.

### З BamDude 0.3.x → 0.4.x

- `m012` додає кластер MFA (6 нових таблиць + `password_changed_at` на users).
- Наявні користувачі тримають свої наявні JWT-токени до наступного логіну. З наступного логіну на сцену виходять sliding-session refresh-токени (TTL access JWT падає з 24 г до 1 г, але ротуючий refresh-cookie тримає вас залогіненими прозоро). Сесії браузера, що були до оновлення, ще не мають refresh-cookie, тож вони почнуть фейлитись на 401, щойно TTL збереженого access-токена в 24 г закінчиться -- у цей момент користувача викидає на `/login` і вмикається новий флоу.
- `m014` бекфілить `library_file_id` на наявних архівах через hash-метчинг і **перезаписує** `library_files.print_count` / `last_printed_at` значеннями, виведеними з історії завершених архівів. Ручні правки цих полів до міграції відкидаються -- історія архівів виграє.
- `m019` -- це рефакторинг queue↔archive; кешовані термінальні лічильники з'їжджають з `printer_queues` і тепер обчислюються при читанні з `print_archives`. Завершені елементи черги авто-видаляються в `on_print_complete`.
- `m021` дропає `printers.auto_light_off`. Якщо ви покладались на цей прапорець, відтворіть поведінку, сконфігурувавши mqtt-action макрос `chamber_light_off` на події `print_started` після оновлення.

### З BamDude 0.4.0 → 0.4.1

- `m022` відкриває кожен 3MF на диску, щоб витягти два нових поля метаданих (`gcode_label_objects`, `exclude_object`). Очікуйте довгий старт на інсталяціях з тисячами архівів -- приблизно **50--200 мс на файл**, кілька хвилин для ферми з кількома тисячами архівів, поки API підніметься.
- Seed комітить пакетами по **100** і логує прогрес кожним пакетом. Слідкуйте за цими рядками логу:

    ```text
    INFO  [backend.app.migrations] m022 library_files: progress 100/847
    INFO  [backend.app.migrations] m022 print_archives: progress 1500/3261
    ```

- 3MF, видалені з диска (рядки історії, чий файл зник), мовчки пропускаються -- ці рядки просто лишаються без нових полів. Міграція повністю one-shot; таблиця `_migrations` запобігає повторним запускам, навіть якщо ви рестартнете посеред.

---

## :material-clipboard-check-multiple: 6. Перевірка після оновлення

Коли сервіс знову на ходу:

1. **`/system/health` повертає 200.**
2. **Параметри → Система → версія** відображає новий реліз.
3. **Підключіться до принтера, який працював до оновлення** -- має реконектнутись за 30 секунд; перевірте картку принтера на сторінці Принтери.
4. **Відкрийте кілька найновіших архівів** -- мініатюри мають усе ще рендеритись, 3D-перегляд -- працювати, клік на іконку принтера -- стрибати на принтер-власник.
5. **Тригерніть диспатч черги** -- тост у нижньому правому куті має показати серіалізований прогрес диспатчу (одне завдання за раз через усю ферму; див. [Черги для кожного принтера → Поведінка диспатчу](../features/print-queue.md#materialdatabasearrowright-povedinka-dyspatchu-odne-zavdannya-za-raz)).
6. **Перелогіньтесь** (якщо оновлюєтесь з 0.3.x → 0.4.x), щоб видали refresh-token cookie і перебрав на себе sliding-session флоу.

Фрагменти лог міграції, які корисно грепнути:

```text
INFO  [backend.app.migrations] Applied m019 (version 19)
INFO  [backend.app.migrations] Applied m022 (version 22)
INFO  [backend.app.main] Startup complete
```

Індикатори падіння:

```text
ERROR  [backend.app.migrations] Migration mXXX failed: ...
sqlite3.OperationalError: no such column: ...
```

`no such column` / `no such table` на старті майже завжди означає, що міграція не виконалась -- зазвичай це проблема дозволів файлової системи на `data/`. Полагодьте через `sudo chown -R bamdude:bamdude /opt/bamdude/data` і перезапустіть.

---

## :material-undo-variant: 7. Відкат (якщо щось зламалось)

Оскільки схема рухається тільки вперед, план відкату завжди -- **відновіть до-оновлювальний backup**. Автоматичної down-міграції немає -- ви не можете, наприклад, "відмінити" m019 archive↔queue рефакторинг на місці. Відновлення з backup -- єдиний шлях.

=== "Docker volumes"

    ```bash
    docker compose down

    # Витерти вміст нового тома
    docker volume rm bamdude_data
    docker volume create bamdude_data

    # Відновити з backup-тарбола
    docker run --rm \
      -v "$(pwd)/backup":/from \
      -v bamdude_data:/to \
      alpine sh -c 'cd /to && tar xzf /from/bamdude-data-YYYYMMDD.tar.gz'

    # Запіньте compose на попередній Docker-тег перед стартом:
    # image: ghcr.io/kainpl/bamdude:0.4.0
    docker compose up -d
    ```

=== "Native (UI backup)"

    На свіжій інсталяції старого тегу, після first-run setup, відкрийте **Налаштування → Backup → Локальна резервна копія**, **Завантажте** скачаний zip, потім перезапустіть сервіс. Zip відновлює БД + архіви + аплоади + конфіг одним заходом.

=== "Native (shell tar)"

    ```bash
    sudo systemctl stop bamdude
    cd /opt/bamdude
    sudo rm -rf data
    sudo tar xzf ~/bamdude-data-YYYYMMDD.tar.gz
    sudo -u bamdude git checkout v0.4.0       # або ваш попередній тег
    sudo -u bamdude ./venv/bin/pip install -r requirements.txt
    sudo systemctl start bamdude
    ```

=== "PostgreSQL"

    ```bash
    docker compose down       # або зупиніть нативний сервіс
    pg_restore -c -d "$DATABASE_URL" ~/bamdude-YYYYMMDD.dump
    # Потім відновіть archive/ + library/ з тарбола даних.
    docker compose up -d      # з image, запіненим на попередній тег
    ```

Версія, на яку ви відкочуєтесь, **має бути тією, що створила backup** -- інакше схема в БД буде новіша за те, що очікує цей код, і старт зафейлиться помилкою column-not-found на першому ж читанні.

!!! info "Forward-only -- це навмисно"
    Down-міграції потребували б шляхів коду, яких BamDude не несе, -- відновлення з backup структурно простіше і завжди коректне. Docker-тег `:0.4.0` лишається запіненим безстроково, тож ви завжди можете відкотитись на нього.

---

## :material-database: 8. Нотатки про backend БД

### SQLite (дефолт)

Файл БД живе за `data/bamdude.db`. Pragma SQLite: WAL journal, 15 с busy timeout, NORMAL synchronous. WAL означає, що поруч із головним файлом є ще `bamdude.db-wal` та `bamdude.db-shm` -- бекапте всі три разом (або зупиніть сервіс перед цим, щоб WAL було чекпойнтнуто в головний файл).

Якщо спадковий `bambuddy.db` (або `bambutrack.db`) існує в директорії даних, але `bamdude.db` -- ні, BamDude перейменовує його при першому завантаженні перед тим, як запуститься хоч одна міграція. Саме так шлях `m000_bambuddy_import` спрацьовує для нативних інсталяцій, що міняють бінарник на місці.

### PostgreSQL

Виставте `DATABASE_URL=postgresql+asyncpg://user:pass@host/db` у вашому оточенні. На першому старті зі **свіжою, порожньою** БД PostgreSQL BamDude авто-мігрує контент з SQLite-файлу, якщо обидва присутні (one-shot копія SQLite → PG). Після копіювання використовується тільки PG; SQLite-файл лишається на місці для безпеки, але до нього більше не доторкаються.

Наявні PG-інсталяції запускають той самий ланцюжок міграцій на кожному старті -- та сама таблиця `_migrations`, ті самі версії, та сама послідовність. Хелпери діалекту маршрутизують DDL через PG-нативні шляхи там, де SQLite потребує `recreate_table` (зміни FK, дроп колонок). PG-сторонні міграції також ензорсять FK-обмеження, які SQLite пропускає мовчки -- `m018` -- хороший приклад, де SET NULL впливає на живу поведінку лише на PG.

---

## :material-bug: Траблшутінг

**Стартовий лог показує `setup_required` 503 на кожному ендпоінті**
: Перше завантаження не створює admin. Відкрийте `/` у браузері, щоб пройти setup-флоу. Це нормально для свіжих інсталяцій і після кожного `cli reset_admin`.

**`no such column` / `no such table` на старті**
: Міграція не виконалась. Перевірте лог на стек-трейс; зазвичай це означає, що файлові дозволи на `data/` не дають сервіс-юзеру писати. Полагодьте через `sudo chown -R bamdude:bamdude /opt/bamdude/data`.

**Імпорт Bambuddy не вистрелив**
: Або `bamdude.db` уже існує (тож файл ніколи не сканувався), або файл не названо `bambuddy.db` / `bambutrack.db`. Перейменуйте і перезапустіть -- перевірка міграції перезапускається на кожному завантаженні, поки не застосується.

**Копіювання Docker volume падає з `device or resource busy`**
: Спочатку зупиніть і source, і destination контейнер. Контейнер alpine з `--rm`, що монтує обидва тома, не може ділити файлову систему з працюючим сервісом, що тримає відкриті файли.

**Native update лишає сервіс зламаним**
: `update.sh` пише backup до того, як щось чіпає (`/opt/bamdude/backups/pre-update-YYYYMMDD-HHMMSS/`). Зупиніть сервіс, відновіть директорію backup поверх `data/` та вичекаутіть попередній git-тег.

**Довга пауза на першому завантаженні 0.4.1**
: Це `m022` обходить кожен 3MF на диску. Хвостіть лог -- ви маєте бачити рядки `m022 library_files: progress N/M` кожним пакетом по 100. Не вбивайте процес; перезапуск просто продовжить з того місця, де відкомітився останній пакет.

**`database is locked` посеред міграції**
: Ви запустили сервіс до того, як попередня інстанція повністю зупинилась. Зупиніть, дочекайтесь виходу старого процесу (перевірте через `pgrep -f bamdude` / `docker compose ps`), потім стартуйте знову. Система міграцій ідемпотентна -- міграції, що впали посеред, чисто перезапускаються при наступному завантаженні.

---

## :material-new-box: Що нового в 0.4.x

| Можливість | Опис |
|---------|------|
| **Per-Printer Queues** | Незалежна черга для кожного принтера з картковим UI; quantity > 1 пропускає кожну копію через чергу (без особливої "primary"). |
| **Рефакторинг queue↔archive** | Жива черга авточиститься; історія черги живе на `print_archives` (m019). |
| **Серіалізований диспатч** | Один диспатч за раз через усю ферму -- усуває гонку SQLite `database is locked` на `INSERT INTO print_archives`. |
| **Sliding-session auth** | TTL access JWT -- 1 г; ротуючий refresh-cookie тримає юзерів залогіненими прозоро. Remember-me опт-іниться на 30-денну персистентність. |
| **MFA + OIDC** | TOTP, email OTP, 10 backup-кодів, OIDC SSO з PKCE + JWKS + SSRF-захистом. Шифрується at rest з `MFA_ENCRYPTION_KEY`. |
| **MQTT-action макроси** | Макроси можуть інвокувати MQTT-команду (`chamber_light_off` / `chamber_light_on`) на `print_started` / `print_finished` з опційною затримкою. Заміняє спадковий прапорець `auto_light_off`. |
| **Per-project print plan** | Кожен проєкт несе впорядкований список своїх `.3mf`-файлів бібліотеки зі степером копій, per-row totals та смугою grand-totals. |
| **Відновлення завантаження 3MF** | Fallback-архіви авто-заповнюються через FTP, коли принтер був недосяжний на старті друку. |
| **Метадані label-object** | Прапорці підтримки skip-objects, витягнуті з `Metadata/project_settings.config` і збережені на кожному файлі бібліотеки + архіві (m022). |

Див. [CHANGELOG.md](https://github.com/kainpl/bamdude/blob/main/CHANGELOG.md) для детальностей по версіях.
