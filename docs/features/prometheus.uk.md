---
title: Метрики Prometheus
description: Експорт телеметрії принтерів для дашбордів Grafana
---

# Метрики Prometheus

BamDude може надавати телеметрію принтерів у форматі Prometheus для інтеграції з **Grafana**, **Prometheus** та іншими системами моніторингу.

---

## :material-cog: Налаштування

Перейдіть до **Налаштування > Мережа > Метрики Prometheus**.

| Параметр | Ключ у БД | Опис | За замовчуванням |
|----------|-----------|------|------------------|
| **Увімкнути метрики** | `prometheus_enabled` | Увімкнення/вимкнення ендпоінту | Вимкнено |
| **Bearer Token** | `prometheus_token` | Необов'язкова Bearer-token автентифікація на `/metrics` | Порожній (відкрито) |

!!! info "Автентифікація на /metrics"
    `/api/v1/metrics` ігнорує звичайний стек автентифікації BamDude — він має власний шлюз. Коли `prometheus_enabled=false`, він повертає 404 (виглядає як неналаштований). Коли увімкнено без `prometheus_token`, він відкритий. Коли увімкнено з токеном, виклики мають надсилати `Authorization: Bearer <token>`. Встановлюйте токен щоразу, коли Prometheus працює на окремому хості, якому ви повністю не довіряєте.

---

## :material-api: Ендпоінт

```
GET /api/v1/metrics
```

Повертає метрики у [текстовому форматі експозиції Prometheus](https://prometheus.io/docs/instrumenting/exposition_formats/).

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" http://bamdude:8000/api/v1/metrics
```

---

## :material-chart-line: Доступні метрики

Кожна метрика на рівні принтера має лейбли `printer_id`, `printer_name` та `serial`. Агрегатні counters/gauges не мають лейблів або мають лейбли `result` / `fan` / `nozzle` залежно від ситуації.

### Build info

| Метрика | Тип | Опис |
|---------|-----|------|
| `bamdude_build_info` | gauge | `version`, `python_version`, `platform`, `architecture` (завжди = 1) |

### Стан окремого принтера

| Метрика | Тип | Опис |
|---------|-----|------|
| `bamdude_printer_connected` | gauge | Статус з'єднання (1/0) |
| `bamdude_printer_state` | gauge | 0=unknown, 1=idle, 2=running, 3=pause, 4=finish, 5=failed, 6=prepare, 7=slicing |
| `bamdude_print_progress` | gauge | Поточний прогрес друку (0-100) |
| `bamdude_print_remaining_seconds` | gauge | Орієнтовний залишковий час (секунди) |
| `bamdude_print_layer_current` | gauge | Номер поточного шару |
| `bamdude_print_layer_total` | gauge | Загальна кількість шарів у поточному друці |

### Температури + вентилятори

| Метрика | Тип | Опис |
|---------|-----|------|
| `bamdude_bed_temp_celsius` | gauge | Поточна температура столу |
| `bamdude_bed_target_celsius` | gauge | Цільова температура столу |
| `bamdude_nozzle_temp_celsius` | gauge | Поточна температура сопла (лейбл `nozzle="0"`/`"1"` для подвійного сопла H2D) |
| `bamdude_nozzle_target_celsius` | gauge | Цільова температура сопла |
| `bamdude_chamber_temp_celsius` | gauge | Температура камери (надається лише для моделей із сенсором) |
| `bamdude_fan_speed_percent` | gauge | Швидкість вентилятора (лейбл `fan="part"`/`"aux"`/`"chamber"`) |
| `bamdude_wifi_signal_dbm` | gauge | Рівень сигналу WiFi у dBm |

### Агрегатні (з БД)

| Метрика | Тип | Опис |
|---------|-----|------|
| `bamdude_prints_total` | counter | Загальна кількість друків за весь час, лейбл `result="completed"`/`"failed"`/тощо |
| `bamdude_printer_prints_total` | counter | Загальна кількість друків за весь час по кожному принтеру |
| `bamdude_filament_used_grams` | counter | Загальна витрата філаменту |
| `bamdude_print_time_seconds` | counter | Загальний задокументований час друку |
| `bamdude_queue_pending` | gauge | Кількість завдань у черзі |
| `bamdude_queue_printing` | gauge | Кількість завдань, які зараз друкуються |
| `bamdude_printers_connected` | gauge | Підключених принтерів зараз |
| `bamdude_printers_total` | gauge | Налаштованих принтерів зараз |

---

## :material-chart-bar: Дашборд Grafana

Додайте BamDude як джерело даних Prometheus у Grafana для створення дашбордів з телеметрією принтерів, прогресом друку, динамікою температур та завантаженістю парку.

---

## :material-lightbulb: Поради

!!! tip "Інтервал scrape"
    Інтервал scrape 15-30 секунд достатній для телеметрії принтерів.

> Початково базується на документації [Bambuddy](https://github.com/maziggy/bambuddy).
