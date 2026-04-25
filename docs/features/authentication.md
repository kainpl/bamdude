---
title: Authentication
description: Always-on authentication with MFA, OIDC SSO, refresh-token sessions, and group-based permissions
---

# Authentication

BamDude ships with always-on authentication: every API endpoint is protected, the first boot walks you through creating an admin, and from there users sign in with passwords, optional 2FA, or OIDC single sign-on. This page is the single source of truth for the auth stack -- groups, sessions, MFA, SSO, rate limits, and recovery.

---

## :material-lock: Overview

- **User accounts** -- multiple users with unique credentials and per-user MFA settings.
- **Group-based permissions** -- 80+ granular `resource:action` permissions, three default groups (Administrators / Operators / Viewers), arbitrary custom groups.
- **Sliding-session JWTs** -- 1 hour access tokens, transparently refreshed via an HttpOnly rotating cookie so users don't get bounced mid-session.
- **Multi-factor authentication** -- TOTP (authenticator apps), email OTP, and 10 single-use backup codes.
- **OIDC / SSO** -- authorization-code flow with PKCE for any standards-compliant provider (Authentik, Keycloak, Pocket-ID, Google Workspace, ...).
- **Rate limiting** -- per-username and per-IP sliding-window buckets on login + forgot-password.
- **Setup-gate + admin recovery** -- fresh installs walk through a one-time setup; lost-all-admins is recoverable via a CLI without losing data.

!!! info "Auth is always on"
    There is no "disable auth" toggle. Every endpoint requires a valid session or API key. API keys (`X-API-Key` or `Authorization: Bearer bb_...`) bypass JWT validation but still satisfy the same permission checks.

---

## :material-rocket-launch: First-Boot Setup

On its very first boot BamDude knows it has no admin yet, so it locks down the API and shows a setup form.

1. Open the BamDude UI. The setup wizard is rendered automatically.
2. Enter the initial admin's **username**, **password**, and (optional) **email**.
3. Submit. Setup-gate flips off; you are redirected to the regular login page and signed in.

While the setup gate is up, only three endpoints respond:

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/auth/status` | Is setup needed? Used by the UI to pick login vs setup. |
| `POST /api/v1/auth/setup` | Create the initial admin. |
| `GET /api/v1/system/health` | Liveness probe. |

Every other call returns `503 {"detail": "setup_required"}` until setup completes.

!!! warning "Don't expose a fresh container"
    The setup endpoint is unauthenticated by design (there is no admin yet to authenticate against). Only expose port 8000 publicly **after** you've completed setup, or run setup over a private network first.

---

## :material-account-group: Default Groups

| Group | Description | Permissions |
|-------|-------------|-------------|
| **Administrators** | Full access | All permissions |
| **Operators** | Control printers and manage content | Printer control, queue, archives, library |
| **Viewers** | Read-only access | View printers, archives, queue |

Custom groups can mix and match permissions. Newly OIDC-linked users land in **Viewers** by default (configurable per provider).

---

## :material-key: Permission Categories

Permissions follow a `resource:action` pattern -- e.g. `printers:control`, `archives:read`. Endpoints declare the permission they need with `RequirePermission(...)` so the matrix is enforced consistently across REST, WebSocket, and Telegram surfaces.

- **Printers** -- read, create, update, delete, control, files, clear_plate
- **Archives** -- read, create, update_own / update_all, delete_own / delete_all, reprint_own / reprint_all
- **Queue** -- read, create, update_own / update_all, delete_own / delete_all, reorder
- **Library** -- read, upload, update_own / update_all, delete_own / delete_all
- **Settings** -- read, update, backup, restore
- **Users / Groups** -- read, create, update, delete

!!! tip "Ownership permissions"
    Use `*_own` permissions for users who should only modify their own uploads and queue items. Operators typically get `*_all`; Viewers get neither.

---

## :material-clock-fast: Session Management

BamDude uses a sliding-session model: short-lived access tokens, long-lived rotating refresh cookie.

### Access tokens

- **TTL:** 1 hour (was 24 h pre-0.4.0).
- **Carry `jti` + `iat`.** Logout revokes the token's `jti` until natural expiry; password changes stamp `users.password_changed_at`, and tokens older than that timestamp are rejected as stale on every request.

### Refresh tokens

- Issued by `/auth/login`, `/auth/2fa/verify`, and `/auth/oidc/exchange`.
- Stored as a SHA-256 hash in `auth_ephemeral_tokens`; delivered to the browser as the `bamdude_refresh` cookie -- **HttpOnly**, **SameSite=Lax**, **Path=/api/v1/auth**. JavaScript never sees it; non-auth endpoints never receive it.
- **Rotated on every use.** `POST /auth/refresh` marks the old row `used_at=now`, mints a new row in the same `family_id`, and returns a fresh access token.
- **OWASP reuse detection.** If a refresh token is replayed (i.e. used twice), BamDude collapses the entire family across every device. The user is forced back to the login page everywhere.
- **Logout / password change / admin-initiated MFA reset** revoke ALL refresh tokens for the user, signing out every device.

### Remember-me

The login form has a **"Remember me for 30 days"** checkbox.

| Mode | DB row TTL | Cookie lifetime |
|------|------------|-----------------|
| Default | 12 hours | Session cookie (cleared when browser closes) |
| Remember me | 30 days | `Max-Age=30d` -- survives browser restarts |

### Frontend behaviour

The frontend `request()` helper transparently retries 401s through `/auth/refresh`, **promise-coalesced** so a wave of parallel queries spawns exactly one refresh call. If refresh also fails, a global `bamdude:auth-invalidated` event clears React state and hard-redirects to `/login`. A visibility-change listener proactively revalidates `/auth/me` when a hidden tab regains focus.

### `Secure` cookie attribute

`Secure` is auto-detected from the request scheme. Behind a reverse proxy, set `TRUSTED_PROXY_IPS` (comma-separated) so BamDude reads the original `X-Forwarded-Proto` header.

```ini
# .env
TRUSTED_PROXY_IPS=10.0.0.1,10.0.0.2
```

For edge cases (e.g. TLS-terminating load balancer that doesn't set `X-Forwarded-Proto`), force the polarity:

=== "Force Secure on"

    ```ini
    AUTH_REFRESH_COOKIE_SECURE=true
    ```

=== "Force Secure off"

    ```ini
    AUTH_REFRESH_COOKIE_SECURE=false
    ```

---

## :material-shield-key: Multi-Factor Authentication

2FA is per-user opt-in. Each user can enrol one or more factors from **Settings -> Profile -> Two-Factor Authentication**.

### Factors

| Factor | How it works |
|--------|--------------|
| **TOTP** | Authenticator app (Google Authenticator, Aegis, Authy, 1Password, ...). Six-digit rolling code, generated from a Fernet-encrypted secret. |
| **Email OTP** | One-time code sent to the user's email. Useful as a fallback when TOTP isn't practical. |
| **Backup codes** | 10 single-use codes generated at enrolment. Shown **once** -- store them offline. Re-generate anytime to invalidate the old set. |

### Login flow with 2FA

1. User submits username + password.
2. Server verifies credentials, returns `requires_2fa=true`, a short-lived `pre_auth_token`, and a 2fa-challenge cookie.
3. UI shows the 2FA picker (TOTP / email / backup code).
4. User submits the code to `/auth/2fa/verify`.
5. Server returns the access JWT + sets the refresh cookie. Login complete.

### Encryption at rest

When `MFA_ENCRYPTION_KEY` is set, TOTP secrets and OIDC client secrets are Fernet-encrypted in the database. Backup codes are pbkdf2-hashed regardless.

```ini
# .env
MFA_ENCRYPTION_KEY=<base64 32-byte Fernet key>
```

!!! tip "Generate a key"
    `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

!!! warning "Plaintext fallback"
    If `MFA_ENCRYPTION_KEY` is unset, BamDude stores secrets in plaintext and logs a warning at boot. Secrets are **prefix-versioned**, so you can enable encryption later -- existing rows transparently re-encrypt on next read; users don't need to re-enrol.

### Admin-initiated reset

If a user loses their authenticator device, an admin can trigger a 2FA reset for that user from the Users page. The reset disables all factors **and revokes every refresh token** for that account, so any logged-in session is killed -- the user signs in fresh with just a password and re-enrols.

---

## :material-account-key: OIDC / SSO

BamDude supports OpenID Connect single sign-on against any standards-compliant provider.

### Configure a provider

**Settings -> Authentication -> OIDC Providers -> Add provider.**

| Field | Notes |
|-------|-------|
| **Display name** | Label on the login button ("Sign in with Authentik"). |
| **Issuer URL** | The provider's discovery URL base (e.g. `https://auth.example.com/`). Must be HTTPS. |
| **Client ID** | From the provider's BamDude app registration. |
| **Client secret** | Fernet-encrypted at rest when `MFA_ENCRYPTION_KEY` is set. |
| **Scopes** | Default `openid profile email`. Add provider-specific scopes if needed. |
| **Claim mapping** | Which OIDC claim maps to BamDude username / email. |
| **Auto-create users** | Off by default -- new logins must match an existing local user by email. On = auto-create in the Viewers group. |

The login page renders an "Sign in with `<provider>`" button per configured provider, below the password form.

### Hardening

- **PKCE S256** -- mandatory, non-negotiable.
- **State + nonce** -- both verified on callback. The state token is atomically consumed, so replays fail.
- **JWKS verification** -- ID tokens are signature-verified against the provider's published JWKS.
- **SSRF guards** -- the issuer URL must be HTTPS and must not resolve to loopback, private (RFC 1918), or link-local addresses.

### Self-signed CAs

If your provider runs behind a self-signed certificate (common for self-hosted Authentik / Keycloak), point BamDude at the CA bundle:

```ini
OIDC_CA_BUNDLE_PATH=/etc/ssl/certs/my-internal-ca.pem
```

!!! warning "Don't auto-link by email lightly"
    Auto-create + auto-link to existing local accounts means a compromised IdP can hijack any local user with a matching email. Leave both off unless you trust the provider as much as your local password hashes.

---

## :material-speedometer: Rate Limiting

Sliding-window buckets sit in front of password-bearing endpoints. Buckets are stored in the `auth_rate_limit_events` table -- no global lock, so legit users on the same network aren't held back by an attacker burning through codes elsewhere.

| Endpoint | Per-username | Per-IP |
|----------|--------------|--------|
| `POST /auth/login` | 10 / 15 min | 20 / 15 min |
| `POST /auth/forgot-password` | 3 / 15 min (per email) | 10 / 15 min |

Forgot-password records the attempt **eagerly** -- the endpoint always returns success (anti-enumeration), so the rate limit is the only thing pacing brute-force email guessing.

### Behind a reverse proxy

If BamDude sits behind nginx / Caddy / Traefik / Cloudflare, set `TRUSTED_PROXY_IPS` so the rate limiter reads the **original client IP** from `X-Forwarded-For` instead of the proxy's IP -- otherwise every request shares the proxy's IP and the cap bites within a few logins.

```ini
# .env -- comma-separated, no spaces
TRUSTED_PROXY_IPS=10.0.0.1,172.16.0.1
```

Multi-hop chains (nginx -> Cloudflare -> BamDude) are handled by right-to-left resolution: BamDude walks `X-Forwarded-For` from the right and accepts the rightmost IP that **isn't** in the trusted set as the real client.

!!! info "Single-host deploys"
    Leave `TRUSTED_PROXY_IPS` unset on a no-proxy install. BamDude falls back to the direct TCP peer IP, which is correct in that case.

---

## :material-form-textbox-password: Password Policy

Aligned with [NIST SP 800-63B](https://pages.nist.gov/800-63-3/sp800-63b.html). Composition rules beyond a sane minimum are deprecated by NIST as low-value friction; BamDude follows that lead.

**On create / change / reset:**

- At least one **uppercase** letter
- At least one **lowercase** letter
- At least one **digit**
- Minimum **8 characters**
- Maximum **256 characters** (sane upper bound to cap pbkdf2 cost)

No special-character requirement (dropped in 0.4.0.1 -- previously enforced, now considered noise that pushes users to predictable substitutions).

Other length caps across auth endpoints: email **254** (RFC 5321), username **150**, forgot-password token **128**.

### Password change kills sessions

Changing your password (or having an admin reset it) stamps `users.password_changed_at`. Any access token with `iat` older than that timestamp is rejected as stale on the next request, and every refresh-token row for that user is revoked. Result: a password change instantly logs you out of every device, the way it should.

---

## :material-tools: Admin Recovery

If you somehow lose access to every admin account -- forgotten password, lost MFA device with no backup codes, deleted the only admin user -- you can reset the setup gate from a shell on the host.

```bash
# Stop the running server first.
docker compose stop bamdude
# OR for native installs:
systemctl stop bamdude

# Run the reset CLI against the same DB the server uses.
python -m backend.app.cli reset_admin

# Restart.
docker compose start bamdude
```

`reset_admin` clears the setup-complete flag and orphan `user_groups` rows so the next boot re-enters the **first-boot setup form**. You'll create a new admin from scratch -- and **all your existing data (printers, archives, queue, users, library) is preserved**. Only the admin account itself is recreated.

!!! warning "Run with the server stopped"
    Both the CLI and the server hold the SQLite WAL. Running them simultaneously can corrupt the database. Stop the server first.

---

## :material-lightbulb: Tips

!!! tip "Enrol TOTP for every admin"
    Admin accounts hold the keys to the farm. TOTP + offline backup codes is the minimum bar for any account that can change settings or delete archives.

!!! tip "Encrypt MFA secrets"
    Set `MFA_ENCRYPTION_KEY` before enrolling users. Plaintext secrets work, but encrypted-at-rest is one less thing on the list when you do your next backup audit.

!!! tip "Use OIDC for teams"
    If you already run Authentik / Keycloak / Pocket-ID for the rest of your homelab, wire BamDude into it -- you get group sync, MFA, and offboarding for free instead of maintaining a parallel password store.

!!! tip "Print the backup codes"
    Backup codes are shown **once**. Print them, drop them in your password manager's secure notes, or both -- but don't trust yourself to remember to write them down later.
