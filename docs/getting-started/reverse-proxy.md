---
title: Reverse proxy & HTTPS
description: Run BamDude behind nginx with HTTPS, optionally keeping plain HTTP available on the LAN
---

# Reverse proxy & HTTPS

This guide covers putting **BamDude behind nginx** so external access goes over HTTPS while you keep raw `http://` on the LAN if you want it. The most common reason to bother: hitting BamDude from outside the workshop without exposing plain HTTP to the internet.

The same instructions adapt to Caddy / Traefik / HAProxy — the headers + WebSocket bits are what BamDude actually cares about, the proxy product is irrelevant.

---

## :material-routes: Three access models

| Mode | URL | nginx? | TLS? | When |
|------|-----|--------|------|------|
| **LAN-only HTTP** | `http://192.168.1.10:8000` | no | no | Home network, single user, no external access |
| **External HTTPS only** | `https://bamdude.example.com` | yes | yes | Always go through the proxy, even on LAN |
| **Hybrid (recommended for farms)** | LAN: `http://192.168.1.10:8000` *and* external: `https://bamdude.example.com` | yes (external only) | external | Direct LAN access for low-latency camera streams + HTTPS from outside |

The hybrid model is what most operators end up wanting. Skip to [Hybrid setup](#hybrid-lan-http-external-https) once you've read the basics — the env vars + nginx config are the same as "External HTTPS only" with one extra hostname rule.

---

## :material-shield-key: How BamDude detects HTTPS

The backend has to know whether a given request arrived over HTTPS so it can set the `Secure` flag on the refresh-token cookie correctly. Browsers refuse to send `Secure` cookies over plain HTTP, so getting this wrong locks users out.

BamDude resolves the `Secure` flag in this order:

1. **Hard override** — `AUTH_REFRESH_COOKIE_SECURE` env var. `true` → always Secure. `false` → never Secure. Unset → auto-detect (recommended; it's what makes the hybrid model possible).
2. **Auto-detect, request scheme** — `request.url.scheme == "https"` → Secure=True.
3. **Auto-detect, trusted proxy** — when the immediate caller's IP is listed in `TRUSTED_PROXY_IPS`, BamDude reads `X-Forwarded-Proto` and uses *that* scheme instead. This is what nginx termination relies on.
4. **Otherwise** → Secure=False. Plain LAN HTTP works fine; the cookie just isn't HTTPS-only.

!!! warning "TRUSTED_PROXY_IPS is required for HTTPS via nginx to work"
    Without `TRUSTED_PROXY_IPS=<nginx-ip>`, BamDude sees `X-Forwarded-Proto: https` from an *untrusted* source and ignores it. Every request looks like plain HTTP, refresh cookies get `Secure=False`, and login works *once* (until refresh) but refresh always fails — users get bumped to `/login` mid-session.

---

## :material-cog: BamDude env vars for proxy setups

Set these in `.env` (or your Docker `environment:` block) before starting BamDude.

```bash
# IPs of every reverse proxy that's allowed to set X-Forwarded-* headers.
# Comma-separated. Use the IP nginx uses to reach BamDude — i.e. on the
# same host this is usually 127.0.0.1; in compose it's the proxy's
# container IP / network alias. NOT the public IP.
TRUSTED_PROXY_IPS=127.0.0.1

# Optional hard override. Leave UNSET for the hybrid mode. Set to "true"
# only when EVERY request reaches BamDude over HTTPS (i.e. nginx is the
# only entrypoint). Set to "false" only on plain-HTTP LAN-only installs.
# AUTH_REFRESH_COOKIE_SECURE=true

# Used by login emails / "click to open BamDude" links. Should be the
# externally-reachable URL — even on hybrid, point this at the HTTPS one
# so links shared via Telegram / email work from anywhere.
APP_URL=https://bamdude.example.com
```

The `Settings → System → External URL` field in the UI is the same value as `APP_URL` env. Whichever is set takes precedence in the order: DB setting > env var > `http://localhost:5173` fallback.

---

## :material-nginx: nginx config

Drop this in `/etc/nginx/sites-available/bamdude` and `ln -s` it into `sites-enabled/`. Ports + paths assume BamDude listens on `127.0.0.1:8000` on the same host as nginx.

```nginx
# HTTP → HTTPS redirect for the public hostname.
server {
    listen 80;
    listen [::]:80;
    server_name bamdude.example.com;
    return 301 https://$host$request_uri;
}

# HTTPS terminator for external access.
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name bamdude.example.com;

    # Standard certbot output. Replace with whatever you use.
    ssl_certificate     /etc/letsencrypt/live/bamdude.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bamdude.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # 3MF / camera frames / archive bundles can be big. Default 1m is too low.
    client_max_body_size 512m;

    # MJPEG camera streams + WebSocket pushes are long-lived; keep the
    # tunnel open. 1h covers any reasonable print state observation.
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;          # camera streams need real-time bytes
    proxy_request_buffering off;

    # Let BamDude see the original scheme + client IP. The Forwarded-Proto
    # is what flips the Secure-cookie auto-detect to True. Without it
    # /auth/refresh fails over HTTPS even though TLS is working.
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host  $host;

    # WebSocket upgrade — BamDude pushes live status / dispatch progress /
    # archive events over /api/v1/ws. Without these headers the upgrade
    # silently fails and the UI just shows stale data.
    proxy_http_version 1.1;
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection "upgrade";

    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

After saving:

```bash
sudo nginx -t                # syntax check
sudo systemctl reload nginx
```

---

## :material-lan: Hybrid: LAN HTTP + external HTTPS

The single proxy block above already does external HTTPS. Two extra steps for hybrid:

**1. Don't force `AUTH_REFRESH_COOKIE_SECURE`.** Leave it unset so auto-detect picks the right polarity per request:

* LAN visitor on `http://192.168.1.10:8000` → cookie not Secure → browser sends it back. Works.
* External visitor on `https://bamdude.example.com` → nginx adds `X-Forwarded-Proto: https`, BamDude trusts the header (nginx is in `TRUSTED_PROXY_IPS`), cookie Secure → browser sends it only on HTTPS. Works.

**2. Use a *different* hostname for HTTPS than for HTTP.** BamDude (correctly) sends `Strict-Transport-Security` on HTTPS responses; the browser caches that for the hostname and refuses HTTP for it afterwards. If both modes share `bamdude.local`, the first HTTPS visit poisons LAN access permanently.

The pragmatic split most operators use:

| Use case | Hostname | Reachable via |
|----------|----------|---------------|
| LAN | `http://192.168.1.10:8000` | direct, IP-based — never gets HSTS |
| External | `https://bamdude.example.com` | nginx, public DNS — HSTS is fine |

If you really want a hostname (not IP) on the LAN too, use a *different* one — `bamdude.lan` or `bamdude.home` — and make sure no client ever sees HTTPS at that name.

---

## :material-bug: Common issues

### Login works but refresh keeps failing → user gets bounced to `/login`

`X-Forwarded-Proto` isn't being honoured because the proxy IP isn't in `TRUSTED_PROXY_IPS`. Check what BamDude sees:

```bash
# Look at the access log inside the container/process
# Should show your nginx host IP, not your laptop IP
```

Set `TRUSTED_PROXY_IPS` to that exact IP and restart BamDude.

### "WebSocket connection failed"

You forgot the `Upgrade` / `Connection: upgrade` headers in nginx. The UI loads but live updates (printer status, queue progress, archive events) are stale until reload.

### Camera stream stops after ~60 s

`proxy_read_timeout` defaults to 60 s in nginx. Bump it (`3600s` in the config above) and add `proxy_buffering off;` so MJPEG bytes flow as they arrive instead of being chunked into nginx's buffer.

### "Mixed content blocked" in browser console

A relative URL somewhere is resolving to `http://`. Most often `Settings → System → External URL` is set to `http://...` while you're accessing over HTTPS. Set it to the `https://` URL or empty (BamDude falls back to `APP_URL` env var).

### LAN HTTP stopped working after I visited HTTPS once

That's HSTS doing its job — your browser locked the hostname to HTTPS. Two fixes:

1. **Use different hostnames** (recommended) — see the table above. IP-based LAN URL solves it cleanly.
2. **Clear HSTS in the browser** — Chrome: `chrome://net-internals/#hsts`, "Delete domain security policies". Firefox: clear site data including history. This works only until you visit HTTPS again.

### Big 3MF uploads return 413

`client_max_body_size 512m;` in the server block — default is 1 MB, way too small.

### `/auth/refresh` 401s but the user just logged in

The refresh cookie made it back to nginx but not to BamDude. Either:

- nginx isn't forwarding cookies — usually a misconfigured `proxy_pass` that strips `Cookie:`. The config above doesn't strip headers; check for explicit `proxy_set_header Cookie ""` somewhere upstream.
- Cookie path mismatch. The refresh cookie has `Path=/api/v1/auth` — your nginx must proxy that path to BamDude (the catch-all `location /` does). If you split routes, make sure `/api/v1/auth/*` lands on the same backend.

---

## :material-check-decagram: Sanity checklist

Before declaring victory:

- [ ] `TRUSTED_PROXY_IPS` set to the IP nginx uses to reach BamDude.
- [ ] `APP_URL` (env) or **External URL** (Settings → System) points at the public HTTPS URL.
- [ ] `proxy_set_header X-Forwarded-Proto $scheme;` present.
- [ ] `proxy_http_version 1.1;` + `Upgrade` + `Connection: upgrade` present.
- [ ] `proxy_read_timeout` bumped (≥600 s; 3600 s for camera streams).
- [ ] `client_max_body_size` raised (≥256 m; 512 m for swap-mode batches).
- [ ] HSTS hostname is **different** from the LAN hostname if you keep both modes.
- [ ] Logged in over HTTPS, hit refresh, F5 the page → still logged in.
- [ ] Open a camera stream, leave it running 10 minutes → still streaming.
- [ ] LAN HTTP URL still works (if hybrid mode).
