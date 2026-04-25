---
title: Reverse-proxy і HTTPS
description: BamDude за nginx з HTTPS, з можливістю залишити HTTP по локальній мережі
---

# Reverse-proxy і HTTPS

Цей гайд про те як поставити **BamDude за nginx**, щоб зовнішній доступ йшов по HTTPS, а локальний по HTTP лишався як був (якщо потрібно). Найпопулярніший привід — заходити в BamDude з-за межі майстерні без виставляння plain HTTP в інтернет.

Те саме адаптується під Caddy / Traefik / HAProxy — BamDude не залежить від проксі-продукту, головне правильні заголовки + WebSocket.

---

## :material-routes: Три моделі доступу

| Режим | URL | nginx? | TLS? | Коли |
|-------|-----|--------|------|------|
| **Тільки LAN, HTTP** | `http://192.168.1.10:8000` | ні | ні | Домашня мережа, один користувач, без зовнішнього доступу |
| **Тільки зовнішній HTTPS** | `https://bamdude.example.com` | так | так | Завжди через проксі, навіть з LAN |
| **Гібрид (рекомендовано для ферм)** | LAN: `http://192.168.1.10:8000` *плюс* зовнішній: `https://bamdude.example.com` | так (тільки для зовнішнього) | зовнішній | Прямий LAN-доступ для камер з малою затримкою + HTTPS ззовні |

Гібрид — те що більшість операторів врешті хочуть. Перейти до [Гібридного налаштування](#hybrid-lan-http-zovnishniy-https) можна після ознайомлення з основами — env-змінні + nginx-конфіг ті самі що й для "Тільки зовнішній HTTPS", плюс одне правило про іменування хостів.

---

## :material-shield-key: Як BamDude визначає HTTPS

Бекенд має знати чи запит прийшов через HTTPS, щоб правильно поставити прапорець `Secure` на refresh-token cookie. Браузери не шлють `Secure` cookie через plain HTTP, тож помилка тут блокує користувачів від входу.

BamDude визначає `Secure` у такому порядку:

1. **Жорстке перекриття** — env-змінна `AUTH_REFRESH_COOKIE_SECURE`. `true` → завжди Secure. `false` → ніколи Secure. Не задано → авто-детект (рекомендовано; це й уможливлює гібридну модель).
2. **Авто-детект, схема запиту** — `request.url.scheme == "https"` → Secure=True.
3. **Авто-детект, довірений проксі** — коли IP найближчого клієнта є в `TRUSTED_PROXY_IPS`, BamDude читає `X-Forwarded-Proto` і використовує *цю* схему. На цьому тримається термінація TLS на nginx.
4. **Інакше** → Secure=False. Plain LAN HTTP працює; cookie просто не обмежений HTTPS.

!!! warning "TRUSTED_PROXY_IPS обов'язково для роботи HTTPS через nginx"
    Без `TRUSTED_PROXY_IPS=<nginx-ip>` BamDude бачить `X-Forwarded-Proto: https` від *недовіреного* джерела і ігнорує. Кожен запит виглядає як plain HTTP, refresh-cookie отримує `Secure=False`, login проходить *один раз* (до першого refresh), а далі refresh завжди фейлиться — користувача викидає на `/login` посеред сесії.

---

## :material-cog: Env-змінні для проксі-сетапу

Прописати в `.env` (або в Docker `environment:`-блоці) ДО запуску BamDude.

```bash
# IP-и всіх reverse-проксі, яким дозволено ставити X-Forwarded-* заголовки.
# Через кому. Це той IP з якого nginx стукається в BamDude — на тому ж
# хості зазвичай 127.0.0.1; в compose це IP контейнера-проксі / network
# alias. НЕ публічний IP.
TRUSTED_PROXY_IPS=127.0.0.1

# Опціональне жорстке перекриття. Для гібридного режиму ЗАЛИШИТИ
# непризначеним. Ставити "true" тільки коли КОЖЕН запит приходить через
# HTTPS (тобто nginx — єдина точка входу). Ставити "false" тільки для
# чистих LAN-only HTTP інсталяцій.
# AUTH_REFRESH_COOKIE_SECURE=true

# Використовується в листах логіну / посиланнях "відкрити BamDude".
# Має бути зовнішньо-доступний URL — навіть на гібриді, скеровувати
# СЮДИ на HTTPS, щоб посилання з Telegram / email працювали звідусіль.
APP_URL=https://bamdude.example.com
```

Поле **Settings → System → External URL** в UI має те саме значення що й env `APP_URL`. Пріоритет: DB-налаштування > env > fallback `http://localhost:5173`.

---

## :material-nginx: Конфіг nginx

Поставити в `/etc/nginx/sites-available/bamdude` і `ln -s` в `sites-enabled/`. Порти + шляхи припускають, що BamDude слухає на `127.0.0.1:8000` на тому ж хості що й nginx.

```nginx
# HTTP → HTTPS редирект для публічного хосту.
server {
    listen 80;
    listen [::]:80;
    server_name bamdude.example.com;
    return 301 https://$host$request_uri;
}

# HTTPS термінатор для зовнішнього доступу.
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name bamdude.example.com;

    # Стандартний вивід certbot. Заміни на те що в тебе.
    ssl_certificate     /etc/letsencrypt/live/bamdude.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bamdude.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # 3MF / кадри камер / архів-бандли можуть бути великі. Дефолтні 1m мало.
    client_max_body_size 512m;

    # MJPEG-стріми + WebSocket-пуші довготривалі; тримати тунель відкритим.
    # 1 година покриває будь-яке адекватне спостереження стану друку.
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;          # стрімам камер потрібні байти в реальному часі
    proxy_request_buffering off;

    # Дати BamDude бачити оригінальну схему + IP клієнта. Forwarded-Proto —
    # саме те, що перемикає авто-детект Secure-cookie на True. Без нього
    # /auth/refresh фейлиться через HTTPS навіть коли TLS працює.
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host  $host;

    # WebSocket upgrade — BamDude шле live-status / dispatch progress /
    # archive events через /api/v1/ws. Без цих заголовків upgrade
    # тихо фейлиться і UI просто показує застарілі дані.
    proxy_http_version 1.1;
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection "upgrade";

    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

Після збереження:

```bash
sudo nginx -t                # перевірка синтаксису
sudo systemctl reload nginx
```

---

## :material-lan: Гібрид: LAN HTTP + зовнішній HTTPS  { #hybrid-lan-http-zovnishniy-https }

Один проксі-блок вище вже робить зовнішній HTTPS. Два додаткових кроки для гібрида:

**1. НЕ ставити `AUTH_REFRESH_COOKIE_SECURE`.** Залишити невизначеним щоб авто-детект сам обирав правильну полярність кожного запиту:

* LAN-відвідувач на `http://192.168.1.10:8000` → cookie без Secure → браузер шле назад. Працює.
* Зовнішній відвідувач на `https://bamdude.example.com` → nginx додає `X-Forwarded-Proto: https`, BamDude довіряє заголовку (nginx у `TRUSTED_PROXY_IPS`), cookie Secure → браузер шле тільки по HTTPS. Працює.

**2. Використовувати *різні* хостнейми для HTTPS і HTTP.** BamDude (правильно) шле `Strict-Transport-Security` на HTTPS-відповіді; браузер кешує це для хостнейму і відмовляється від HTTP для нього потім. Якщо обидва режими шарять `bamdude.local`, перший HTTPS-візит назавжди отруїть LAN-доступ.

Прагматичний розподіл який застосовують більшість:

| Кейс | Хостнейм | Доступ через |
|------|----------|--------------|
| LAN | `http://192.168.1.10:8000` | прямий, по IP — HSTS не отримує |
| Зовнішній | `https://bamdude.example.com` | nginx, публічний DNS — HSTS ОК |

Якщо реально хочеш hostname (не IP) і на LAN — використовуй *інший* — `bamdude.lan` або `bamdude.home` — і пильнуй щоб жоден клієнт ніколи не побачив HTTPS на цьому імені.

---

## :material-bug: Часті проблеми

### Логін проходить, але refresh постійно фейлиться → користувача викидає на `/login`

`X-Forwarded-Proto` не приймається бо IP проксі немає в `TRUSTED_PROXY_IPS`. Перевір що бачить BamDude:

```bash
# Подивитись access-log у контейнері/процесі
# Має показувати IP nginx-хосту, а не свого ноутбука
```

Прописати `TRUSTED_PROXY_IPS` рівно до цього IP і перезапустити BamDude.

### "WebSocket connection failed"

Забув `Upgrade` / `Connection: upgrade` заголовки в nginx. UI завантажується, але live-апдейти (статус принтерів, прогрес черги, події архіву) застарілі до перезавантаження.

### Стрім камери падає через ~60 сек

`proxy_read_timeout` за замовчуванням 60 сек у nginx. Підняти (`3600s` у конфігу вище) і додати `proxy_buffering off;` щоб MJPEG-байти йшли як приходять, а не накопичувались у буфері nginx.

### "Mixed content blocked" в консолі браузера

Десь відносний URL резолвиться у `http://`. Найчастіше **Settings → System → External URL** виставлено на `http://...` поки ти заходиш по HTTPS. Прописати `https://`-URL або очистити (BamDude підпадне на env `APP_URL`).

### LAN HTTP перестав працювати після одного візиту по HTTPS

Це HSTS робить своє — браузер залочив хостнейм на HTTPS. Два варіанти:

1. **Різні хостнейми** (рекомендовано) — див. таблицю вище. IP-LAN-URL вирішує чисто.
2. **Очистити HSTS у браузері** — Chrome: `chrome://net-internals/#hsts`, "Delete domain security policies". Firefox: очистити дані сайту разом з історією. Допомагає тільки до наступного HTTPS-візиту.

### Великі 3MF аплоади повертають 413

`client_max_body_size 512m;` у server-блоці — за замовчуванням 1 МБ, дуже мало.

### `/auth/refresh` віддає 401 хоч щойно залогінився

Refresh-cookie дійшов до nginx але не до BamDude. Або:

- nginx не форвардить cookie — зазвичай помилка в `proxy_pass` що зрізає `Cookie:`. Конфіг вище заголовки не зрізає; шукай явні `proxy_set_header Cookie ""` десь вище по ланцюжку.
- Path mismatch у cookie. Refresh-cookie має `Path=/api/v1/auth` — твій nginx має проксіювати цей шлях у BamDude (загальний `location /` робить це). Якщо ти роздробив роути, переконайся що `/api/v1/auth/*` йде на той самий бекенд.

---

## :material-check-decagram: Чек-ліст санітарний

Перед оголошенням перемоги:

- [ ] `TRUSTED_PROXY_IPS` стоїть на IP з якого nginx стукається в BamDude.
- [ ] `APP_URL` (env) або **External URL** (Settings → System) на публічному HTTPS URL.
- [ ] `proxy_set_header X-Forwarded-Proto $scheme;` присутній.
- [ ] `proxy_http_version 1.1;` + `Upgrade` + `Connection: upgrade` присутні.
- [ ] `proxy_read_timeout` піднятий (≥600 сек; 3600 сек для камер).
- [ ] `client_max_body_size` піднятий (≥256m; 512m для swap-mode-батчів).
- [ ] HSTS-хостнейм **відрізняється** від LAN-хостнейма якщо тримаєш обидва режими.
- [ ] Залогінився по HTTPS, рефреш сторінки, F5 → залишився залогінений.
- [ ] Відкрити стрім камери, лишити на 10 хв → стрімить.
- [ ] LAN-HTTP-URL досі працює (на гібриді).
