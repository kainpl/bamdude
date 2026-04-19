---
title: Підтримувані принтери
description: Інформація про сумісність моделей принтерів Bambu Lab
---

# Підтримувані принтери

BamDude підтримує всі 3D-принтери Bambu Lab з можливістю Developer Mode.

---

## :material-check-circle: Підтримувані моделі

| Модель | Серія | Камера | AMS |
|--------|-------|:------:|:---:|
| **X1** | X1 | :material-check: | :material-check: |
| **X1 Carbon** | X1 | :material-check: | :material-check: |
| **X1E** | X1 | :material-check: | :material-check: |
| **H2D** | H2 | :material-check: | :material-check: |
| **H2D Pro** | H2 | :material-check: | :material-check: |
| **H2C** | H2 | :material-check: | :material-check: |
| **H2S** | H2 | :material-check: | :material-check: |
| **P1P** | P1 | Додатково | :material-check: |
| **P1S** | P1 | :material-check: | :material-check: |
| **P2S** | P2 | :material-check: | :material-check: |
| **A1** | A1 | :material-check: | AMS Lite |
| **A1 Mini** | A1 | :material-check: | :material-close: |

---

## :material-printer-3d: Особливості за серіями

### Серія X1

- Обігрів камери
- До 4 блоків AMS
- Повна підтримка камери (до 30 FPS)

### Серія H2

- H2D / H2D Pro: подвійне сопло зі статусом лівого/правого сопла
- H2C: стійка для 6 сопел (tool changer)
- Обігрів камери

### Серія P1

- P1P потребує додаткової камери
- До 4 блоків AMS
- Камера обмежена ~5 FPS

### Серія A1

- A1: підтримка AMS Lite
- A1 Mini: лише зовнішня котушка, основна ціль для [режиму заміни](../features/swap-mode.md)
- Камера обмежена ~5 FPS

---

## :material-connection: Вимоги до підключення

Для всіх принтерів потрібно:

- **Developer Mode** увімкнений (надає доступ через LAN)
- **SD-карта** встановлена (для передачі файлів)
- **Та сама мережа**, що й сервер BamDude

| Порт | Протокол | Призначення |
|------|----------|-------------|
| 8883 | MQTT/TLS | Зв'язок з принтером |
| 990 | FTPS | Передача файлів |

> Початково базується на документації [Bambuddy](https://github.com/maziggy/bambuddy).
