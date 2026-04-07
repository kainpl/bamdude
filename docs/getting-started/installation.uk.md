---
title: Встановлення
description: Встановлення BamDude на вашу систему
---

# Встановлення

Цей посібник описує ручне встановлення BamDude. Для Docker (рекомендовано) дивіться [посібник з Docker](docker.uk.md).

---

## :material-check-all: Вимоги

| Вимога | Деталі |
|--------|--------|
| **Python** | 3.10+ (рекомендується 3.11 або 3.12) |
| **Мережа** | Та сама локальна мережа, що й принтер Bambu Lab |
| **Принтер** | Увімкнений Developer Mode ([інструкція](index.uk.md#developer-mode)) |
| **SD-карта** | Вставлена в принтер (потрібна для передачі файлів) |

!!! tip "Альтернатива -- Docker"
    Якщо ви віддаєте перевагу контейнерам, перегляньте [посібник зі встановлення Docker](docker.uk.md) -- це ще простіше!

---

## :material-download: Ручне встановлення

=== ":material-ubuntu: Ubuntu/Debian"

    ```bash
    # Встановлення залежностей
    sudo apt update
    sudo apt install python3 python3-venv python3-pip git

    # Клонування та налаштування
    git clone https://github.com/kainpl/bamdude.git
    cd bamdude
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

    # Запуск
    uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
    ```

=== ":material-apple: macOS"

    ```bash
    # Встановлення залежностей (за потреби)
    brew install python@3.12

    # Клонування та налаштування
    git clone https://github.com/kainpl/bamdude.git
    cd bamdude
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

    # Запуск
    uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
    ```

Відкрийте [http://localhost:8000](http://localhost:8000) у браузері.

---

## :material-tune: Конфігурація

Налаштуйте BamDude за допомогою змінних середовища або файлу `.env`:

```bash
cp .env.example .env
nano .env
```

### Змінні середовища

| Змінна | За замовчуванням | Опис |
|--------|-----------------|------|
| `DEBUG` | `false` | Увімкнення режиму налагодження (детальне логування) |
| `LOG_LEVEL` | `INFO` | Рівень логування: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_TO_FILE` | `true` | Запис логів у `logs/bamdude.log` |

---

## :material-cog: Запуск як сервіс

=== ":material-linux: systemd (Linux)"

    Створіть файл сервісу:

    ```bash
    sudo nano /etc/systemd/system/bamdude.service
    ```

    ```ini
    [Unit]
    Description=BamDude Print Farm Manager
    After=network.target

    [Service]
    Type=simple
    User=YOUR_USERNAME
    Group=YOUR_USERNAME
    WorkingDirectory=/home/YOUR_USERNAME/bamdude
    Environment="PATH=/home/YOUR_USERNAME/bamdude/venv/bin"
    ExecStartPre=-/usr/bin/pkill -9 ffmpeg
    ExecStopPost=-/usr/bin/pkill -9 ffmpeg
    ExecStart=/home/YOUR_USERNAME/bamdude/venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
    Restart=always
    RestartSec=10

    [Install]
    WantedBy=multi-user.target
    ```

    Активація та запуск:

    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable bamdude
    sudo systemctl start bamdude
    ```

---

## :material-network: Мережеві вимоги

| Порт | Протокол | Напрямок | Призначення |
|------|----------|----------|-------------|
| 8000 | HTTP | Вхідний | Веб-інтерфейс BamDude |
| 8883 | MQTT/TLS | Вихідний | Зв'язок з принтером |
| 990 | FTPS | Вихідний | Передача файлів з принтера |

---

## :material-folder-cog: Збірка frontend з вихідного коду

Репозиторій містить попередньо зібрані файли frontend. Для збірки з вихідного коду:

```bash
cd frontend
npm install
npm run build
cd ..
```

---

## :checkered_flag: Наступні кроки

<div class="quick-start" markdown>

[:material-printer-3d: **Додайте принтер**<br><small>Підключіть свій перший принтер</small>](first-printer.uk.md)

[:material-docker: **Спробуйте Docker**<br><small>Ще простіше налаштування</small>](docker.uk.md)

[:material-help-circle: **Вирішення проблем**<br><small>Проблеми зі встановленням?</small>](../reference/troubleshooting.md)

</div>

> Базується на документації [Bambuddy](https://github.com/maziggy/bambuddy).
