---
title: Віртуальний принтер
description: Емуляція принтера Bambu для надсилання друків зі слайсера, з review/auto-dispatch маршрутами і підтримкою PASV-NAT
---

# Віртуальний принтер

Віртуальний принтер (VP) робить так, що BamDude з'являється у вашій LAN як один або кілька принтерів Bambu Lab. "Send to Printer" з Bambu Studio / OrcaSlicer лягає на VP так само, як він лягав би на справжній принтер — через захищений TLS (MQTT + FTPS) з access-кодом принтера. Далі BamDude роутить аплоад згідно з режимом VP.

---

## :material-printer-3d: Огляд

Кожен VP:

- Анонсує себе через **SSDP** з реальним кодом моделі Bambu (X1C / P1S / A1 Mini / H2D / …), щоб слайсери виявляли його автоматично.
- Запускає **власні FTPS + MQTT + SSDP сервери**, прив'язані до виділеного IP. Кілька VP працюють поряд без портових конфліктів — у кожного власний IP, а не власний порт.
- Несе **access-код**, як справжній принтер — слайсери питають його при першому використанні і кешують потім.
- Має **серійний номер** і **код моделі**, що збігаються з реальним форматом Bambu, — тож compatibility checks слайсера проходять.

---

## :material-swap-horizontal: Режими

| Режим | Що відбувається з аплоадами | Use case |
|-------|----------------------------|----------|
| **immediate** | Файл парситься, і відразу створюється рядок `print_archives`. Нічого не друкується. | Чистий print-архів — слайсер це джерело, BamDude це каталог. |
| **file_manager** | Файл лягає в **бібліотеку** (`/library`) для пізнішого використання. | Збирання бібліотеки, з якої будете диспатчити пізніше — вручну або через чергу. |
| **print_queue** | Файл архівується **і** додається в чергу друку. Auto-dispatch + цільовий принтер роблять це one-click workflow. | Найпоширеніший production-режим: slice → send → BamDude друкує. |
| **proxy** | TCP-проксується безпосередньо в реальний принтер за TLS-endpoint-ом BamDude. | Віддалений друк — слайсер говорить з BamDude через LAN/VPN, BamDude говорить з принтером. |

---

## :material-cog: Налаштування

**Налаштування → Virtual Printer → Add Virtual Printer**:

| Поле | Примітки |
|------|----------|
| Name | Display-лейбл (наприклад, `Studio inbox`). |
| Model | SSDP model code — оберіть модель принтера, яку VP має імперсонувати, щоб compatibility checks слайсера проходили. |
| Bind IP | Виділений IP для цього VP. На Linux найпростіший шлях — virtual interface (alias) на хості. |
| Access code | 8-символьний код, яким автентифікується слайсер. |
| Mode | Один з чотирьох вище. |
| Auto-dispatch | Тільки для режиму `print_queue` — див. нижче. |
| Target printer | Тільки для режиму `proxy` — реальний принтер, на який пробрасувати. |

Слайсери виявляють новий VP через SSDP автоматично за хвилину-дві. Якщо discovery не спрацював, додайте вручну за IP + access-кодом.

---

## :material-clipboard-check: Review-модалка (режим file_manager)

У режимі `file_manager` кожен завантажений 3MF лягає в **review queue** на `/pending-uploads`. З review-модалки оператор:

1. Відкриває аплоад, бачить розпарсений metadata + мініатюру.
2. Обирає цільовий реальний принтер.
3. Перевіряє AMS slot mapping, вибір плити і будь-які per-print опції.
4. Натискає **Send to Printer** — 3MF диспатчиться через стандартний background-dispatch pipeline (FTP-аплоад, swap macros, archive linkage).

Review-батчі також можна **архівувати масово** (без друку, просто заскладувати metadata) або **відхилити** (видаляє аплоад). Корисно, коли кілька юзерів / машин слайсять у один і той же VP, і ви хочете sanity-check, перш ніж це справді доб'ється до принтера.

API: `GET /api/v1/pending-uploads/`, `POST /api/v1/pending-uploads/{id}/archive`, `POST /api/v1/pending-uploads/archive-all`.

---

## :material-flash: Auto-dispatch (режим print_queue)

VP у режимі `print_queue` з `auto_dispatch=true` повністю пропускає review:

- Слайсер "Send to Printer" → VP приймає аплоад через FTPS
- VP створює queue item, цільований на реальний принтер за вашою політикою
- Background dispatch підбирає його як будь-який інший queue item — без operator-кроку

Поставте `auto_dispatch=false`, якщо хочете, щоб кожен поставлений у чергу аплоад чекав explicit Start-кліку в queue UI перед диспатчем.

!!! tip "Тільки trusted upstream"
    Auto-dispatch прибирає human gate. Використовуйте його, коли upstream-джерело — це ви самі або trusted-автоматизація (slicer plugin, CI job, MakerWorld webhook). Для shared / multi-tenant аплоадів краще режим `file_manager` + review-модалка.

---

## :material-network-outline: PASV Address (NAT / Docker bridge)

FTPS використовує команду PASV — сервер каже клієнту, на який IP передзвонити для data-каналу. Коли BamDude працює в Docker bridge мережі (або за будь-яким NAT), PASV-відповідь інакше анонсувала б **внутрішній IP контейнера** — слайсери в LAN не зможуть до нього достукатися, і data-канал зафейлиться посеред handshake-у.

Поставте env-змінну `VIRTUAL_PRINTER_PASV_ADDRESS` на **externally-reachable IP** (LAN-адресу хоста — більшість слайсерів тут не резолвлять hostnames):

```bash
VIRTUAL_PRINTER_PASV_ADDRESS=192.168.1.100
```

FTPS-сервер стартує, логує `FTP PASV address override: 192.168.1.100`, і відтепер кожна PASV-відповідь використовує цю адресу. Не має ефекту, коли BamDude крутиться на host-мережі — там не задавайте.

---

## :material-rocket: Use cases

- **Архівування друку без друку** — режим `immediate` перетворює slice → send на постійний запис (мініатюри, metadata, source 3MF) без коміту до друку.
- **Збирання бібліотеки** — `file_manager` лягає файлами в бібліотеку, тож ви можете прикріплювати їх до проєктів, batch-друкувати або шарити з командою до першого білда.
- **Multi-user farm inbox** — `file_manager` + review-модалка дозволяє кільком людям слайсити у той же VP, не наступаючи одне одному на ноги.
- **Hands-off диспатч** — `print_queue` + `auto_dispatch=true` — це найближче, що ви отримаєте до "Cloud Print, але локально".
- **Віддалений друк** — режим `proxy` пробрасує remote-слайсера TLS-сесію прямо в реальний принтер, з сертифікатом BamDude як публічним обличчям.

---

## :material-lightbulb: Поради

!!! tip "Один VP на workflow"
    Ніщо не заважає крутити кілька VP одночасно на різних IP — один на production auto-dispatch, один на review, один на архівування. Вони шарять той самий backend, тож усі дані залишаються уніфікованими.

!!! tip "Slicer auth caching"
    Bambu Studio / OrcaSlicer кешують access-код per discovered принтер. Поверніть VP access-код — і слайсери знову спитають, без ручної очистки кешу.

> Початково базується на документації [Bambuddy](https://github.com/maziggy/bambuddy).
