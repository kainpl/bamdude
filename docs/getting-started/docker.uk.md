---
title: Встановлення Docker
description: Розгорніть BamDude за допомогою Docker однією командою
---

# Встановлення Docker

Docker -- найпростіший спосіб запустити BamDude. Одна команда -- і готово.

---

## :rocket: Швидкий старт

=== ":material-download: Готовий образ"

    ```bash
    mkdir bamdude && cd bamdude
    curl -O https://raw.githubusercontent.com/kainpl/bamdude/main/docker-compose.yml
    docker compose up -d
    ```

=== ":material-source-branch: Збірка з вихідного коду"

    ```bash
    git clone https://github.com/kainpl/bamdude.git
    cd bamdude
    docker compose up -d --build
    ```

Відкрийте [http://localhost:8000](http://localhost:8000) у браузері.

---

## :material-cog: Конфігурація

### docker-compose.yml

```yaml
services:
  bamdude:
    image: ghcr.io/kainpl/bamdude:latest
    build: .
    container_name: bamdude
    network_mode: host
    volumes:
      - bamdude_data:/app/data
      - bamdude_logs:/app/logs
    environment:
      - TZ=Europe/Berlin
    restart: unless-stopped

volumes:
  bamdude_data:
  bamdude_logs:
```

### Змінні середовища

| Змінна | За замовчуванням | Опис |
|--------|-----------------|------|
| `TZ` | `UTC` | Ваш часовий пояс (наприклад, `America/New_York`) |
| `PORT` | `8000` | Порт, на якому працює BamDude |
| `DEBUG` | `false` | Увімкнення логування налагодження |
| `LOG_LEVEL` | `INFO` | Рівень логування: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## :material-database: Збереження даних

| Том | Призначення |
|-----|-------------|
| `bamdude.db` | База даних SQLite з усіма даними друку |
| `archive/` | Архівовані файли 3MF та мініатюри |
| `logs/` | Логи застосунку |

!!! tip "Резервне копіювання"
    Для резервного копіювання даних просто скопіюйте ці файли/директорії. Дивіться [Резервне копіювання та відновлення](../features/backup.md) для вбудованої функції.

---

## :material-update: Оновлення

=== ":material-download: Готовий образ"

    ```bash
    docker compose pull && docker compose up -d
    ```

=== ":material-source-branch: Зібраний з вихідного коду"

    ```bash
    cd bamdude && git pull && docker compose build --pull && docker compose up -d
    ```

---

## :material-server: Розширені налаштування

### Зворотний проксі (Nginx)

```nginx
server {
    listen 443 ssl http2;
    server_name bamdude.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}
```

!!! warning "Підтримка WebSocket"
    Переконайтеся, що ваш зворотний проксі підтримує з'єднання WebSocket -- це необхідно для оновлень стану принтера в реальному часі.

### Мережевий режим Host

Мережевий режим host **обов'язковий** для виявлення принтерів та потокового відео з камери на Linux:

```yaml
services:
  bamdude:
    network_mode: host
```

!!! note "macOS / Windows"
    Docker Desktop на macOS та Windows потребує перенаправлення портів замість режиму host. Використовуйте `ports: ["8000:8000"]` та додавайте принтери вручну за IP-адресою.

---

## :material-help-circle: Вирішення проблем

### Контейнер не запускається

```bash
docker compose logs bamdude
```

### Не вдається підключитися до принтера

```bash
docker compose exec bamdude ping YOUR_PRINTER_IP
```

Якщо використовуєте bridge мережевий режим, спробуйте `network_mode: host`.

---

## :checkered_flag: Наступні кроки

<div class="quick-start" markdown>

[:material-printer-3d: **Додайте принтер**<br><small>Підключіть свій перший принтер</small>](first-printer.uk.md)

[:material-arrow-up-circle: **Оновлення**<br><small>Міграція з Bambuddy</small>](upgrading.uk.md)

[:material-help-circle: **Вирішення проблем**<br><small>Виникли проблеми?</small>](../reference/troubleshooting.md)

</div>

> Базується на документації [Bambuddy](https://github.com/maziggy/bambuddy).
