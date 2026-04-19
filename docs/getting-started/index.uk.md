---
title: Початок роботи
description: Запустіть BamDude за кілька хвилин
---

# Початок роботи

Ласкаво просимо до BamDude! Цей посібник допоможе вам швидко налаштувати систему керування фермою друку.

---

## :rocket: Швидке встановлення

=== ":material-docker: Docker (рекомендовано)"

    ```bash
    git clone https://github.com/kainpl/bamdude.git
    cd bamdude
    docker compose up -d
    ```

    Відкрийте [http://localhost:8000](http://localhost:8000) у браузері.

    [:material-arrow-right: Повний посібник з Docker](docker.uk.md)

=== ":material-language-python: Python"

    ```bash
    git clone https://github.com/kainpl/bamdude.git
    cd bamdude
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
    ```

    Відкрийте [http://localhost:8000](http://localhost:8000) у браузері.

    [:material-arrow-right: Повний посібник зі встановлення](installation.uk.md)

---

## :footprints: Наступні кроки

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### :material-numeric-1-circle: Увімкніть Developer Mode
Увімкніть Developer Mode на принтері та запишіть код доступу.

[:material-arrow-right: Інструкція](#developer-mode)
</div>

<div class="feature-card" markdown>
### :material-numeric-2-circle: Додайте принтер
Введіть IP-адресу, код доступу та серійний номер принтера.

[:material-arrow-right: Додати перший принтер](first-printer.uk.md)
</div>

<div class="feature-card" markdown>
### :material-numeric-3-circle: Починайте друк!
BamDude автоматично архівує кожен друк та керує чергою.

[:material-arrow-right: Можливості](../features/index.uk.md)
</div>

</div>

---

## Увімкнення Developer Mode

BamDude підключається до принтера через **Developer Mode** -- локальне з'єднання, що забезпечує повний контроль без інтернету.

!!! info "Навіщо Developer Mode?"
    Developer Mode забезпечує пряме з'єднання між BamDude та принтером через локальну мережу:

    - :material-check: **Працює офлайн** -- Інтернет не потрібен
    - :material-check: **Повний контроль** -- Запуск/зупинка друку, завантаження файлів, керування підсвіткою
    - :material-check: **Дані залишаються локально** -- Жодної залежності від хмари

!!! warning "Developer Mode та LAN Only Mode"
    Починаючи з оновлення прошивки від січня 2025 року, стандартний LAN Only Mode (без Developer Mode) надає лише доступ **тільки для читання**. **Developer Mode обов'язковий** для повної функціональності з BamDude.

### Крок 1: Увімкніть LAN Only Mode

1. На сенсорному екрані принтера перейдіть до **Settings**
2. Відкрийте розділ **Network** або **WLAN**
3. Увімкніть **LAN Only Mode** -- встановіть у положення **ON**

### Крок 2: Увімкніть Developer Mode

1. Після увімкнення LAN Only Mode з'явиться опція **Developer Mode**
2. Увімкніть **Developer Mode** -- встановіть у положення **ON**
3. Запишіть **Access Code**, що відобразиться (8 символів)

!!! warning "Зміна коду доступу"
    Код доступу змінюється щоразу, коли ви вимикаєте та вмикаєте ці режими. Якщо ви повторно увімкнете Developer Mode, потрібно буде оновити код доступу в BamDude.

### Крок 3: Вставте SD-карту

!!! warning "SD-карта обов'язкова"
    Для коректної роботи BamDude в принтер має бути вставлена SD-карта. Вона потрібна для передачі файлів, запуску друку та архівування завершених завдань.

### Крок 4: Зберіть інформацію про принтер

Вам знадобляться такі дані для додавання принтера:

| Інформація | Де знайти |
|-----------|-----------|
| **IP-адреса** | Settings :material-arrow-right: Network |
| **Серійний номер** | Settings :material-arrow-right: Device Info |
| **Код доступу** | Відображається при увімкненні Developer Mode |

---

## :checkered_flag: Що далі?

<div class="quick-start" markdown>

[:material-printer-3d: **Додайте принтер**<br><small>Підключіть свій перший принтер</small>](first-printer.uk.md)

[:material-archive: **Архівування друку**<br><small>Як працює автоматичне архівування</small>](../features/archiving.md)

[:material-bell-ring: **Сповіщення**<br><small>Отримуйте сповіщення на телефон</small>](../features/notifications.md)

[:material-help-circle: **Вирішення проблем**<br><small>Виникли проблеми?</small>](../reference/troubleshooting.md)

</div>

> Базується на документації [Bambuddy](https://github.com/maziggy/bambuddy).
