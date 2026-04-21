---
title: Головна
description: BamDude - Самостійно розгорнута система керування фермою 3D-друку для принтерів Bambu Lab
hide:
  - navigation
  - toc
---

<style>
.md-typeset h1 { display: none; }
</style>

<div class="hero" markdown>

<div markdown>

# Ваша ферма.<br>Ваші дані.<br>Ваш контроль.

**BamDude** -- це самостійно розгорнута система керування фермою 3D-друку для принтерів Bambu Lab. Відстежуйте свій парк принтерів у реальному часі, керуйте чергою та плануйте друк, автоматизуйте робочі процеси за допомогою макросів та контролюйте все через Telegram-бот.

Hard fork проєкту [Bambuddy](https://github.com/maziggy/bambuddy) з індивідуальними чергами для кожного принтера, swap mode, поетапним запуском, Telegram-ботом, макросами, історією обслуговування та багато іншого.

<div class="stats-row" markdown>
  <span class="stat-badge" markdown>:material-printer-3d: Мультипринтер</span>
  <span class="stat-badge" markdown>:material-cloud-off-outline: Працює офлайн</span>
  <span class="stat-badge" markdown>:material-open-source-initiative: Відкритий код</span>
</div>

[Почати :material-arrow-right:](getting-started/index.uk.md){ .btn .btn-primary }
[GitHub :material-github:](https://github.com/kainpl/bamdude){ .btn .btn-secondary }

</div>

</div>

---

## :rocket: Швидкий старт

<div class="quick-start" markdown>

[:material-download: **Встановлення**<br><small>Запустіть за кілька хвилин</small>](getting-started/installation.uk.md)

[:material-docker: **Docker**<br><small>Розгортання однією командою</small>](getting-started/docker.uk.md)

[:material-printer-3d: **Додати принтер**<br><small>Підключіть свій перший принтер</small>](getting-started/first-printer.uk.md)

[:material-arrow-up-circle: **Оновлення**<br><small>Міграція з Bambuddy</small>](getting-started/upgrading.uk.md)

</div>

---

## :sparkles: Можливості

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### :material-monitor-dashboard: Моніторинг у реальному часі
Статус принтерів у реальному часі через WebSocket, потокове відео з камери MJPEG, відстеження помилок HMS та моніторинг вологості й температури AMS.
</div>

<div class="feature-card" markdown>
### :material-clock-outline: Індивідуальні черги для принтерів
Незалежні черги друку для кожного принтера з перетягуванням, планованим друком, поетапним запуском та swap mode для A1 Mini з підміною платформ.
</div>

<div class="feature-card" markdown>
### :material-archive: Архів друку
Автоматичне архівування 3MF з витягуванням метаданих, 3D-попереднім переглядом моделей, виявленням дублікатів та повнотекстовим пошуком.
</div>

<div class="feature-card" markdown>
### :material-robot: Telegram-бот
Повне керування принтером з Telegram з вбудованими меню, авторизацією кількох чатів, рольовими дозволами та інтерактивними сповіщеннями.
</div>

<div class="feature-card" markdown>
### :material-code-braces: Макроси
G-code макроси, що активуються подіями друку (старт, завершення, пауза). Вбудований редактор з налаштуванням для кожного принтера та моделі.
</div>

<div class="feature-card" markdown>
### :material-bell-ring: Сповіщення
Багатопровайдерні сповіщення через WhatsApp, Telegram, Discord, Email, Pushover, ntfy та власні webhook. Підтримка тихих годин та щоденного дайджесту.
</div>

</div>

[Усі можливості :material-arrow-right:](features/index.uk.md){ .md-button }

---

## :printer: Підтримувані принтери

| Серія | Моделі |
|-------|--------|
| **X1 Series** | X1, X1 Carbon, X1E |
| **H2 Series** | H2D, H2D Pro, H2C, H2S |
| **P1 Series** | P1P, P1S |
| **P2 Series** | P2S |
| **A1 Series** | A1, A1 Mini |

---

## :wrench: Технічний стек

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### Backend
:material-language-python: Python
:material-api: FastAPI
:material-database: SQLAlchemy + SQLite
</div>

<div class="feature-card" markdown>
### Frontend
:material-react: React
:material-language-typescript: TypeScript
:material-tailwind: Tailwind CSS
</div>

<div class="feature-card" markdown>
### Комунікація
:material-transit-connection-variant: MQTT over TLS
:material-folder-network: FTPS
:material-web: WebSocket
</div>

</div>

---

<div style="text-align: center; margin-top: 3rem;" markdown>
<span style="opacity: 0.6;">Створено з :heart: для спільноти 3D-друку</span>
</div>
