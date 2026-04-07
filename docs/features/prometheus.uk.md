---
title: Метрики Prometheus
description: Експорт телеметрії принтерів для дашбордів Grafana
---

# Метрики Prometheus

BamDude може надавати телеметрію принтерів у форматі Prometheus для інтеграції з **Grafana**, **Prometheus** та іншими системами моніторингу.

---

## :material-cog: Налаштування

Перейдіть до **Налаштування > Мережа > Метрики Prometheus**.

| Параметр | Опис | За замовчуванням |
|----------|------|------------------|
| **Увімкнути метрики** | Увімкнення/вимкнення ендпоінту | Вимкнено |
| **Bearer Token** | Необов'язкова автентифікація | Порожній |

---

## :material-api: Ендпоінт

```
GET /api/v1/metrics
```

Повертає метрики у [текстовому форматі Prometheus](https://prometheus.io/docs/instrumenting/exposition_formats/).

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" http://bamdude:8000/api/v1/metrics
```

---

## :material-chart-line: Доступні метрики

| Метрика | Тип | Опис |
|---------|-----|------|
| `bamdude_printer_connected` | gauge | Статус з'єднання (1/0) |
| `bamdude_printer_state` | gauge | Стан принтера (idle, running тощо) |
| `bamdude_print_progress` | gauge | Поточний прогрес друку (0-100%) |
| `bamdude_print_remaining_seconds` | gauge | Орієнтовний час, що залишився |
| `bamdude_nozzle_temp` | gauge | Температура сопла |
| `bamdude_bed_temp` | gauge | Температура столу |
| `bamdude_chamber_temp` | gauge | Температура камери |

---

## :material-chart-bar: Дашборд Grafana

Додайте BamDude як джерело даних Prometheus у Grafana для створення дашбордів з телеметрією принтерів, прогресом друку, динамікою температур та завантаженістю парку.

---

## :material-lightbulb: Поради

!!! tip "Інтервал збору"
    Інтервал збору даних 15-30 секунд достатній для телеметрії принтерів.

> Початково базується на документації [Bambuddy](https://github.com/maziggy/bambuddy).
