---
title: Оновлення
description: Оновлення з Bambuddy або попередніх версій BamDude
---

# Оновлення

Цей посібник описує міграцію з Bambuddy на BamDude та оновлення між версіями BamDude.

---

## :material-swap-horizontal: Міграція з Bambuddy 2.x

BamDude -- це hard fork Bambuddy. Ваша існуюча база даних та налаштування сумісні.

### Міграція Docker

1. **Зупиніть Bambuddy:**

    ```bash
    cd bambuddy
    docker compose down
    ```

2. **Створіть резервну копію даних:**

    ```bash
    cp -r bambuddy_data bambuddy_data_backup
    ```

3. **Клонуйте BamDude:**

    ```bash
    git clone https://github.com/kainpl/bamdude.git
    cd bamdude
    ```

4. **Скопіюйте ваші дані:**

    Вкажіть у новому `docker-compose.yml` томи на існуючі дані Bambuddy або скопіюйте директорію даних:

    ```bash
    docker volume create bamdude_data
    docker run --rm -v bambuddy_data:/from -v bamdude_data:/to alpine cp -a /from/. /to/
    ```

5. **Запустіть BamDude:**

    ```bash
    docker compose up -d
    ```

6. **Перевірте:** Відкрийте [http://localhost:8000](http://localhost:8000) та переконайтеся, що принтери й архіви відображаються.

!!! warning "Спочатку резервна копія"
    Завжди створюйте резервну копію даних перед міграцією. Міграція є односторонньою -- BamDude може застосувати міграції бази даних, що несумісні з Bambuddy.

### Ручна міграція (Python)

1. Зупиніть сервіс Bambuddy
2. Створіть резервну копію `bambuddy.db` та директорії `archive/`
3. Клонуйте BamDude та налаштуйте venv
4. Скопіюйте `bambuddy.db` до директорії BamDude
5. Скопіюйте `archive/` до директорії BamDude
6. Запустіть BamDude -- міграції бази даних виконаються автоматично при запуску

---

## :material-arrow-up-circle: Оновлення BamDude

### Docker

```bash
docker compose pull && docker compose up -d
```

### Ручне (Python)

```bash
cd bamdude
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
```

Потім перезапустіть сервіс:

```bash
sudo systemctl restart bamdude
```

---

## :material-new-box: Що нового в BamDude

BamDude додає такі можливості поверх Bambuddy:

| Можливість | Опис |
|-----------|------|
| **Per-Printer Queues** | Незалежна черга для кожного принтера з карточним інтерфейсом |
| **Staggered Start** | Поетапний запуск серійних друків для уникнення піків споживання |
| **Swap Mode** | Підтримка підміни платформ A1 Mini з swap-файлами та макросами |
| **Macros** | G-code макроси, що активуються подіями друку |
| **Telegram Bot** | Повне керування принтером з Telegram через вбудовані меню |
| **Multi-Chat Auth** | Ролі, дозволи та режими реєстрації для кожного чату |
| **Maintenance History** | Детальне логування обслуговування з типами для конкретних моделей |
| **Authentication** | Гранульований рольовий контроль доступу (80+ дозволів) |

---

## :material-database: Сумісність бази даних

- BamDude використовує той самий формат бази даних SQLite, що й Bambuddy
- Міграції бази даних виконуються автоматично при першому запуску
- Ручний SQL не потрібен
- Існуючі принтери, архіви, налаштування та елементи черги зберігаються

!!! tip "Перевірте логи"
    Після оновлення перевірте логи на наявність повідомлень про міграцію:

    ```bash
    docker compose logs --tail 50 bamdude
    ```
