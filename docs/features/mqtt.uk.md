---
title: Публікація MQTT
description: Публікація подій до зовнішніх MQTT брокерів
---

# Публікація MQTT

BamDude може публікувати події до зовнішнього MQTT брокера, що дозволяє інтеграцію з **Home Assistant**, **Node-RED** та іншими системами на базі MQTT.

---

## :material-cog: Налаштування

Перейдіть до **Налаштування > Мережа > Публікація MQTT**.

| Параметр | Опис | За замовчуванням |
|----------|------|------------------|
| **Увімкнути MQTT** | Увімкнення/вимкнення публікації | Вимкнено |
| **Адреса брокера** | Адреса MQTT брокера | -- |
| **Порт** | Порт брокера | 1883 (8883 з TLS) |
| **Ім'я користувача** | Автентифікація (необов'язково) | -- |
| **Пароль** | Автентифікація (необов'язково) | -- |
| **Префікс топіків** | Префікс для всіх топіків | `bamdude` |
| **Використовувати TLS** | Увімкнення шифрування TLS/SSL | Вимкнено |

---

## :material-broadcast: Топіки, що публікуються

Усі топіки мають налаштований вами префікс (за замовчуванням: `bamdude`).

### Події принтера

| Топік | Опис |
|-------|------|
| `bamdude/printers/{serial}/status` | Стан принтера в реальному часі (з обмеженням частоти) |
| `bamdude/printers/{serial}/print/started` | Друк розпочато |
| `bamdude/printers/{serial}/print/completed` | Друк завершено |
| `bamdude/printers/{serial}/print/failed` | Друк не вдався |
| `bamdude/printers/{serial}/ams/changed` | Зміна філаменту в AMS |

### Події черги

| Топік | Опис |
|-------|------|
| `bamdude/queue/added` | Завдання додано до черги |
| `bamdude/queue/started` | Завдання почало друкуватися |
| `bamdude/queue/completed` | Завдання завершено |

---

## :material-home-assistant: Приклад для Home Assistant

```yaml
mqtt:
  sensor:
    - name: "Printer Status"
      state_topic: "bamdude/printers/YOUR_SERIAL/status"
      value_template: "{{ value_json.state }}"
```

---

## :material-lightbulb: Поради

!!! tip "Огляд топіків"
    Використовуйте MQTT Explorer для перегляду опублікованих топіків та розуміння структури повідомлень.

> Початково базується на документації [Bambuddy](https://github.com/maziggy/bambuddy).
