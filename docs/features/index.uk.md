---
title: Можливості
description: Ознайомтеся з усіма можливостями BamDude
---

# Можливості

BamDude має безліч функцій для керування вашою фермою 3D-друку. Ознайомтеся з ними нижче.

---

## :material-printer-3d: Принтери та моніторинг

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-monitor-dashboard: Моніторинг у реальному часі](monitoring.uk.md)
Статус принтерів у реальному часі, температури, прогрес друку та відстеження помилок HMS через WebSocket.
</div>

<div class="feature-card" markdown>
### [:material-camera: Потокове відео з камери](camera.uk.md)
MJPEG потокове відео та знімки з вбудованої камери принтера.
</div>

<div class="feature-card" markdown>
### [:material-water-percent: AMS та вологість](ams.uk.md)
Моніторинг слотів AMS, рівнів вологості та температури. Віддалена сушка та налаштовувані пресети.
</div>

</div>

---

## :material-clock-outline: Черга друку

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-printer-3d-nozzle: Індивідуальні черги](print-queue.uk.md)
Незалежна черга для кожного принтера з перетягуванням, кількістю в партії, запланованим запуском та призначенням за моделлю.
</div>

<div class="feature-card" markdown>
### [:material-timer-sand: Поетапний запуск](staggered-start.md)
Поетапний запуск серійних друків для уникнення піків споживання від одночасного нагрівання столів.
</div>

<div class="feature-card" markdown>
### [:material-swap-horizontal: Swap Mode](swap-mode.md)
Підтримка підміни платформ A1 Mini з swap-файлами, макросами та автоматичним очищенням платформи.
</div>

</div>

---

## :material-archive: Архів друку

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-archive-outline: Архівування друку](archiving.md)
Автоматичне архівування 3MF з витягуванням метаданих, 3D-попереднім переглядом та виявленням дублікатів.
</div>

<div class="feature-card" markdown>
### [:material-folder: Файловий менеджер](file-manager.md)
Перегляд, завантаження та керування локальною бібліотекою файлів друку. Друк напряму або додавання до черги.
</div>

</div>

---

## :material-robot: Автоматизація

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-power-plug: Розумні розетки](smart-plugs.md)
Інтеграція з Tasmota, Home Assistant, REST/Webhook та MQTT для автоматичного ввімкнення/вимкнення та моніторингу енергоспоживання.
</div>

<div class="feature-card" markdown>
### [:material-printer-3d: Віртуальний принтер](virtual-printer.md)
Емуляція принтера Bambu у вашій мережі для надсилання друків безпосередньо зі слайсера.
</div>

<div class="feature-card" markdown>
### [:material-code-braces: Макроси](macros.md)
G-code макроси, що активуються подіями друку, з вбудованим редактором.
</div>

<div class="feature-card" markdown>
### [:material-bell-ring: Сповіщення](notifications.md)
Багатопровайдерні сповіщення через WhatsApp, Telegram, Discord, Email та інше.
</div>

</div>

---

## :material-send: Telegram-бот

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-robot: Налаштування бота](telegram-bot.md)
Повне керування принтером з Telegram через вбудовані меню, друк з бібліотеки та інтерактивні сповіщення.
</div>

<div class="feature-card" markdown>
### [:material-shield-lock: Авторизація кількох чатів](telegram-auth.md)
Авторизація для кожного чату з ролями, дозволами, режимами реєстрації та маршрутизацією сповіщень.
</div>

</div>

---

## :material-wrench: Обслуговування та налаштування

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-tools: Трекер обслуговування](maintenance.md)
Планування та відстеження завдань обслуговування з нагадуваннями за інтервалом та детальною історією.
</div>

<div class="feature-card" markdown>
### [:material-lock: Автентифікація](authentication.md)
Необов'язкова автентифікація користувачів з рольовим контролем доступу та 80+ гранульованими дозволами.
</div>

<div class="feature-card" markdown>
### [:material-backup-restore: Резервне копіювання та відновлення](backup.md)
Повне резервне копіювання та відновлення бази даних для захисту даних.
</div>

</div>

---

## :material-puzzle: Інтеграції

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### [:material-spool: Spoolman](spoolman.md)
Синхронізація інвентарю філаменту зі Spoolman для повного відстеження котушок.
</div>

<div class="feature-card" markdown>
### [:material-wifi: MQTT Publishing](mqtt.md)
Публікація подій до зовнішніх MQTT-брокерів для Home Assistant та Node-RED.
</div>

<div class="feature-card" markdown>
### [:material-chart-line: Prometheus Metrics](prometheus.md)
Експорт телеметрії принтерів для дашбордів Grafana та систем моніторингу.
</div>

</div>
