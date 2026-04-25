---
title: API Reference
description: REST API integration guide for BamDude — auth, permissions, rate limits, endpoint groups, and recipes.
---

# API Reference

BamDude exposes a versioned REST API at `/api/v1` plus a WebSocket channel for realtime printer events. Everything the web UI does is also available to your scripts and integrations.

---

## :material-rocket-launch: Quick start

- **Base URL:** `https://<your-bamdude-host>/api/v1`
- **Interactive docs:** [`/docs`](#) (Swagger UI) and [`/redoc`](#) (ReDoc)
- **OpenAPI schema:** `/openapi.json` — drop into Postman, Insomnia, or any OpenAPI-compatible client
- **Realtime channel:** `wss://<your-bamdude-host>/api/v1/ws`

All endpoints return JSON unless explicitly noted (camera streams, 3MF downloads, and thumbnails return binary). Errors follow the FastAPI shape:

```json
{ "detail": "Not authenticated" }
```

`422` validation errors return arrays of field-level issues:

```json
{
  "detail": [
    { "loc": ["body", "name"], "msg": "field required", "type": "value_error.missing" }
  ]
}
```

!!! tip "Use `/docs` first"
    The interactive Swagger UI is generated from the live server, so it always reflects the routes, schemas, and required permissions of the version you're running. Treat this page as orientation; treat `/docs` as ground truth.

---

## :material-key: Authentication methods

BamDude supports two authentication mechanisms. Both enforce the same permission checks. API keys are matched first; if neither header is present, the request falls through to JWT.

=== "API key (recommended for scripts)"

    Generate keys in **Settings → System → API Keys**. They look like `bb_<random_token>` and never auto-expire — revoke them per-key when no longer needed.

    Send via either header:

    ```bash
    # Preferred: dedicated header
    curl -H "X-API-Key: bb_abc123..." \
      https://bamdude.example.com/api/v1/printers/

    # Equivalent: bearer scheme
    curl -H "Authorization: Bearer bb_abc123..." \
      https://bamdude.example.com/api/v1/printers/
    ```

    Each key carries its own permission set — a subset of the issuing user's permissions. Revoking a user revokes their keys.

=== "JWT session token (used by the web UI)"

    The browser flow:

    ```bash
    curl -X POST -H "Content-Type: application/json" \
      -d '{"username": "admin", "password": "...", "remember_me": true}' \
      https://bamdude.example.com/api/v1/auth/login
    ```

    Response:

    ```json
    {
      "access_token": "eyJhbGc...",
      "token_type": "bearer",
      "requires_2fa": false,
      "user": { "id": 1, "username": "admin", "...": "..." }
    }
    ```

    Send the access token via `Authorization: Bearer <jwt>`. **Access tokens live 1 hour.** A refresh token is set as an HttpOnly cookie (`bamdude_refresh`) on the path `/api/v1/auth` — call `POST /api/v1/auth/refresh` to mint a new access token transparently. Cookie attributes:

    | Attribute | Value |
    |-----------|-------|
    | Path      | `/api/v1/auth` (must be preserved by your client) |
    | HttpOnly  | yes — never exposed to JavaScript |
    | SameSite  | `Lax` |
    | Secure    | auto-detected from request scheme; honors `X-Forwarded-Proto` behind a trusted proxy. Force with the `AUTH_REFRESH_COOKIE_SECURE` env var. |
    | Max-Age   | 30 days when `remember_me=true`; otherwise session cookie + 12 h DB lifetime |

    !!! warning "2FA flow"
        When `requires_2fa: true`, the login response also includes `pre_auth_token` and a 2FA challenge cookie. POST it to `/api/v1/auth/2fa/verify` together with the user's TOTP code (or a backup code) to obtain the access + refresh tokens.

    OIDC SSO follows the same pattern via `/api/v1/auth/oidc/exchange` (PKCE S256 + state + nonce).

---

## :material-speedometer: Rate limiting

BamDude rate-limits authentication endpoints to slow down credential stuffing. Other endpoints are not rate-limited at the API layer — front them with a reverse proxy or firewall if you need a hard ceiling.

| Endpoint | Per-user / email | Per-IP |
|----------|------------------|--------|
| `POST /auth/login` | 10 / 15 min per username | 20 / 15 min |
| `POST /auth/forgot-password` | 3 / 15 min per email | 10 / 15 min |

When the limit trips you receive `429 {"detail": "..."}` and a `Retry-After` header.

!!! tip "Behind a reverse proxy"
    Set `TRUSTED_PROXY_IPS` (comma-separated trusted hops) so rate-limiting reads the real client IP from `X-Forwarded-For` instead of the proxy's IP. See [Reverse Proxy & HTTPS](../getting-started/reverse-proxy.md) for the full nginx / Caddy / Traefik recipes.

---

## :material-shield-lock: Setup gate

On first boot the server only accepts three endpoints. Every other request returns `503 {"detail": "setup_required"}` until an admin is created.

| Endpoint | Purpose |
|----------|---------|
| `GET  /api/v1/auth/status` | Returns `{is_setup, requires_setup, ...}` so installers can detect the empty-DB state. |
| `POST /api/v1/auth/setup`  | One-shot: creates the initial admin and returns the access + refresh tokens. |
| `GET  /api/v1/system/health` | Liveness probe (always whitelisted). |

Once setup completes the gate disables itself in-process; you don't need to restart.

!!! danger "Lost all admins?"
    Run `python -m backend.app.cli reset_admin` on the server to clear the setup flags, then visit the UI to re-enter the setup flow. See [Authentication recovery](../features/authentication.md) for the full recovery protocol.

---

## :material-lock-check: Permissions

Every endpoint is gated by `RequirePermission(Permission.X)` where `X` follows the `resource:action` pattern. There are **80+ permissions** defined in `backend/app/core/permissions.py`. Common ones:

| Resource | Examples |
|----------|----------|
| Printers | `printers:read`, `printers:control`, `printers:create`, `printers:delete`, `printers:files`, `printers:clear_plate` |
| Archives | `archives:read`, `archives:create`, `archives:update_own`, `archives:update_all`, `archives:delete_own`, `archives:delete_all`, `archives:reprint_own`, `archives:reprint_all` |
| Library  | `library:read`, `library:upload`, `library:update_own`, `library:delete_all`, `library:notes_write` |
| Queue    | `queue:read`, `queue:create`, `queue:update_all`, `queue:delete_all`, `queue:reorder` |
| Users    | `users:read`, `users:create`, `users:update`, `users:delete` |
| Settings | `settings:read`, `settings:update`, `settings:backup`, `settings:restore` |
| Camera   | `camera:view` |

Three default groups cover most setups:

- **Administrators** — every permission.
- **Operators** — full control of printers, queue, archives, library; no settings/users administration.
- **Viewers** — read-only.

Create custom groups for granular control. The interactive `/docs` browser shows the required permission for each endpoint.

---

## :material-routes: Endpoint groups

The 43 route modules under `backend/app/api/routes/` are registered under the `/api/v1` prefix. Major groups at a glance:

| Prefix | What it does | Notable endpoints |
|--------|--------------|-------------------|
| `/auth/*` | Login, refresh, setup, OIDC | `login`, `refresh`, `logout`, `setup`, `2fa/verify`, `oidc/exchange`, `forgot-password` |
| `/users/*`, `/groups/*`, `/api-keys/*`, `/mfa/*` | Users, groups, API keys, MFA enrollment | CRUD, group assignment, MFA reset, backup codes |
| `/printers/*`, `/printer-queues/*`, `/cloud/*`, `/discovery/*` | Printer + AMS + Bambu Cloud | status, control, AMS RFID, snapshot, stream-token, network discovery |
| `/archives/*` | Print history | list, get, reprint, delete, **`retry-download`**, **`cleanup/preview`**, **`cleanup/run`**, **`cleanup/status`** |
| `/queue/*`, `/background-dispatch/*` | Queue management + dispatch | add, reorder, cancel, set-status, dispatch state |
| `/library/*`, `/library-notes/*`, `/pending-uploads/*` | File manager | upload, list, delete, add-to-queue, slicer-uploads inbox |
| `/projects/*` | Project grouping | CRUD, print plan, archives by project |
| `/macros/*` | G-code + MQTT-action macros | CRUD, execute |
| `/notifications/*`, `/notification-templates/*`, `/user-notifications/*`, `/telegram/*` | Outbound channels | provider CRUD, template overrides, test send, Telegram bot config |
| `/spoolman/*`, `/inventory/*` | Spool tracking | sync, slot mapping, color/spool catalog |
| `/smart-plugs/*` | Smart plug config | CRUD, energy snapshots, manual on/off |
| `/system/*`, `/support/*`, `/updates/*`, `/firmware/*` | Health + diagnostics + updates | `health`, settings, backup, restore, debug bundle, firmware check |
| `/local-backup/*`, `/git-backup/*` | Backup providers | run, restore, schedule |
| `/maintenance/*`, `/kprofiles/*` | Service tracking + K-profiles | log, due, CRUD |
| `/external-links/*`, `/ams-history/*` | Misc UX | dashboard links, AMS slot change history |
| `/metrics`, `/webhook/*`, `/obico/*`, `/virtual-printers/*` | Integrations | Prometheus metrics, inbound webhooks, Obico AI, virtual printer (slicer target) |

The full enumerated list is in `/docs` — this table just shows where to look.

---

## :material-camera-iris: Camera streams and binary endpoints

Some endpoints can't accept `Authorization` headers because they're consumed by `<img>` / `<video>` tags. They use a short-lived **stream-token** (60 min TTL) passed as a query parameter.

```bash
# 1. Mint a token (auth required)
TOKEN=$(curl -s -H "X-API-Key: bb_..." \
  -X POST https://bamdude.example.com/api/v1/printers/camera/stream-token \
  | jq -r .token)

# 2. Use it on binary endpoints
curl "https://bamdude.example.com/api/v1/printers/2/camera/snapshot?token=$TOKEN" -o snap.jpg
```

Endpoints behind the stream-token gate:

| Endpoint | Returns |
|----------|---------|
| `GET /printers/{id}/camera/stream?token=...` | MJPEG stream |
| `GET /printers/{id}/camera/snapshot?token=...` | JPEG snapshot |
| `GET /printers/{id}/cover?token=...` | Current print cover thumbnail (served from local archive — never triggers an FTP fetch) |
| `GET /printers/{id}/plate-detection-reference?token=...` | Reference image used by plate-clear detection |
| `GET /obico/cached-frame/{nonce}` | Frame URL handed to the Obico ML API. Whitelisted in the auth middleware because Obico's GET can't carry a bearer header — the nonce itself is the capability. |

The web UI keeps the stream token cached per session and refreshes it before expiry.

---

## :material-bell-ring: Webhooks and realtime events

BamDude does **not** expose outbound webhooks for application events. Use a [notification provider](../features/notifications.md) (Telegram, Discord, ntfy, Pushover, Email, Home Assistant) when you need a one-way push.

The realtime channel is the WebSocket at `wss://<host>/api/v1/ws`. It carries:

- Printer status updates (temps, progress, AMS state)
- Dispatch and queue progress
- Archive create / update events
- Smart-plug state and energy ticks

Authenticate the WebSocket the same way as REST — the access token is accepted via the `Authorization` header during the upgrade handshake.

---

## :material-script-text-play: Common operations — quick recipes

### List the last 50 archives for a printer

```bash
curl -H "X-API-Key: bb_..." \
  "https://bamdude.example.com/api/v1/archives/?printer_id=2&page=1&per_page=50"
```

### Add a library file to the queue (3 copies, fixed AMS mapping)

```bash
curl -X POST \
  -H "X-API-Key: bb_..." \
  -H "Content-Type: application/json" \
  -d '{
    "library_file_id": 42,
    "queue_id": 2,
    "ams_mapping": [0, 1, 2, 3],
    "quantity": 3
  }' \
  "https://bamdude.example.com/api/v1/queue/"
```

### Get current printer status

```bash
curl -H "X-API-Key: bb_..." \
  "https://bamdude.example.com/api/v1/printers/2/status"
```

### Skip specific objects mid-print

The body is a JSON array of object IDs reported by the slicer.

```bash
curl -X POST \
  -H "X-API-Key: bb_..." \
  -H "Content-Type: application/json" \
  -d '[100, 200]' \
  "https://bamdude.example.com/api/v1/printers/2/print/skip-objects"
```

### Recover a missing 3MF for an archive

When `on_print_start` couldn't FTP the 3MF (printer unreachable, FTP timeout) the archive row is created with `extra_data.no_3mf_available = true`. Background sweeps retry automatically; you can also trigger it manually:

```bash
curl -X POST \
  -H "X-API-Key: bb_..." \
  "https://bamdude.example.com/api/v1/archives/123/retry-download"
```

### Trigger archive 3MF cleanup (preview, then run)

The cleanup job removes 3MF binaries for archives older than the retention window (the metadata row is kept). The daily cron runs automatically; for an ad-hoc sweep:

```bash
# Dry-run preview — what would be deleted, total bytes
curl -H "X-API-Key: bb_..." \
  "https://bamdude.example.com/api/v1/archives/cleanup/preview"

# Run it
curl -X POST -H "X-API-Key: bb_..." \
  "https://bamdude.example.com/api/v1/archives/cleanup/run"

# Inspect the daily cron's last run + next run
curl -H "X-API-Key: bb_..." \
  "https://bamdude.example.com/api/v1/archives/cleanup/status"
```

### Refresh a JWT session from your own client

```bash
# /auth/refresh reads the HttpOnly bamdude_refresh cookie set during login.
# --cookie-jar / --cookie persists it across calls.
curl -c cookies.txt -X POST \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "..."}' \
  https://bamdude.example.com/api/v1/auth/login

curl -b cookies.txt -c cookies.txt -X POST \
  https://bamdude.example.com/api/v1/auth/refresh
```

The refresh response returns a new access token and rotates the refresh cookie in place. Replaying an already-used refresh token revokes the entire token family across devices (OWASP reuse detection).

---

## :material-code-json: Versioning and stability

- The API version prefix is `/api/v1`. Breaking changes will ship under `/api/v2` rather than mutating v1.
- Additive changes (new endpoints, new optional fields) land in patch / minor releases without notice. Pin the BamDude container version if you depend on response shape stability.
- Deprecations are announced in the [changelog](../about/changelog.md) at least one minor release before removal.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy).
