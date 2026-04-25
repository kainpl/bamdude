---
title: Installation
description: Install BamDude on your system, including the first-boot auth setup gate
---

# Installation

This guide covers installing BamDude manually. For Docker (recommended), see the [Docker guide](docker.md).

---

## :material-check-all: Requirements

| Requirement | Details |
|------------|---------|
| **Python** | 3.10+ (3.11 or 3.12 recommended) |
| **Network** | Same LAN as your Bambu Lab printer |
| **Printer** | Developer Mode enabled ([see guide](index.md#enabling-developer-mode)) |
| **SD Card** | Inserted in the printer (required for file transfers) |

!!! tip "Docker Alternative"
    If you prefer containers, check out the [Docker installation guide](docker.md) -- it's even simpler!

---

## :material-download: Manual Install

=== ":material-ubuntu: Ubuntu/Debian"

    ```bash
    # Install prerequisites
    sudo apt update
    sudo apt install python3 python3-venv python3-pip git

    # Clone and setup
    git clone https://github.com/kainpl/bamdude.git
    cd bamdude
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

    # Run
    uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
    ```

=== ":material-apple: macOS"

    ```bash
    # Install prerequisites (if needed)
    brew install python@3.12

    # Clone and setup
    git clone https://github.com/kainpl/bamdude.git
    cd bamdude
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

    # Run
    uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
    ```

Open [http://localhost:8000](http://localhost:8000) in your browser.

---

## :material-tune: Configuration

Configure BamDude using environment variables or a `.env` file:

```bash
cp .env.example .env
nano .env
```

### Environment Variables

#### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `DEBUG` | `false` | Enable debug mode (verbose logging; in dev also re-runs the latest migration on every boot) |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_TO_FILE` | `true` | Write logs to `logs/bamdude.log` |
| `DATA_DIR` | `<repo>/data` | Override the persistent-data directory (DB + archives + plate calibration) |
| `LOG_DIR` | `<repo>/logs` | Override the log directory |
| `PORT` | `8000` | Port the bundled `python -m backend.app.main` entrypoint binds to |
| `TZ` | system | Timezone string passed to Python (e.g. `Europe/Kyiv`) |

#### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | unset (SQLite) | Postgres URL, e.g. `postgresql+asyncpg://user:pass@host:5432/bamdude`. See [PostgreSQL Support](../features/postgresql.md). |

#### Auth & reverse-proxy

| Variable | Default | Description |
|----------|---------|-------------|
| `JWT_SECRET_KEY` | auto-generated and persisted under `data/` | Override the JWT signing key. Don't change this on a running install or every issued token will be invalidated. |
| `TRUSTED_PROXY_IPS` | empty | Comma-separated reverse-proxy IPs whose `X-Forwarded-For` is trusted (right-to-left resolution). Required behind nginx for accurate per-IP rate limiting. |
| `AUTH_REFRESH_COOKIE_SECURE` | unset (auto-detect) | Force `Secure` polarity on the refresh-token cookie. Auto-detect from the request scheme is the right default; set `true` to force, `false` to disable (LAN HTTP dev only). |
| `MFA_ENCRYPTION_KEY` | unset | URL-safe base64 Fernet key. When set, TOTP secrets and OIDC client secrets are encrypted at rest. Plaintext fallback works without it but logs a warning at boot. |
| `APP_URL` | `http://localhost:5173` | Public-facing URL of BamDude. Used in WebAuthn / passkey RP-ID resolution and in notification deep-links. |

#### Integrations (optional)

| Variable | Description |
|----------|-------------|
| `HA_URL`, `HA_TOKEN` | Home Assistant base URL + long-lived token, used by the HA notification provider. |
| `VIRTUAL_PRINTER_PASV_ADDRESS` | Override the FTP-PASV address advertised by the virtual printer (set this if BamDude runs behind NAT and slicers can't reach the bind IP). |

---

## :material-account-key: First-Boot Setup

BamDude has authentication **always on** — there is no "no-auth" mode. On the very first start the API rejects every request with `503 {"detail": "setup_required"}` until the initial admin user is created. The whitelist that bypasses the gate is exactly three routes (`/api/v1/auth/status`, `/api/v1/auth/setup`, `/api/v1/system/health`), so login and every other endpoint stay closed until setup completes.

### :material-web: Setup wizard (browser)

Open BamDude in a browser. The frontend reads `/api/v1/auth/status`, sees `requires_setup=true`, and renders the setup form:

| Field | Required | Notes |
|-------|----------|-------|
| Username | yes | Becomes the first admin. |
| Password | yes | Min 8 chars, must include upper + lower + digit. Stored as a bcrypt hash. |
| Email | optional | Used for password-reset flows + email-OTP MFA later. |

Submit creates the admin, drops the setup gate, and signs you in. The form never shows again — once any admin exists, navigating to `/setup` redirects to `/login`.

### :material-api: Setup over API

Scripts and bootstrap automation can `POST /api/v1/auth/setup` directly:

```bash
curl -X POST http://localhost:8000/api/v1/auth/setup \
  -H "Content-Type: application/json" \
  -d '{"admin_username":"admin","admin_password":"ChangeMe123","admin_email":"ops@example.com"}'
```

The endpoint is one-shot — once any admin exists, subsequent calls return `403 Forbidden` with `"Setup has already been completed."`. Calls before setup don't need a token; calls after setup must use a JWT.

### :material-key-remove: Recovery — lost all admins

If every admin account is deleted or disabled and nobody can sign in any more, run the rescue CLI to clear the setup-completed flag. The next boot re-enters the wizard. **All other data is preserved** — only the gate flag is cleared.

=== ":material-server: Native install"

    ```bash
    cd /path/to/bamdude
    source venv/bin/activate
    python -m backend.app.cli reset_admin
    ```

=== ":material-docker: Docker"

    ```bash
    docker compose exec bamdude python -m backend.app.cli reset_admin
    ```

The CLI refuses to run while at least one admin still exists — delete the dead accounts directly in the DB first (or via the admin UI if you have any working admin left), then re-run.

!!! tip "Full auth docs"
    Sessions, refresh-token rotation, MFA (TOTP / email OTP / backup codes), OIDC, LDAP, API keys, and rate limiting all live in [Authentication](../features/authentication.md). The setup gate is just step zero.

---

## :material-cog: Running as a Service

=== ":material-linux: systemd (Linux)"

    Create the service file:

    ```bash
    sudo nano /etc/systemd/system/bamdude.service
    ```

    ```ini
    [Unit]
    Description=BamDude Print Farm Manager
    After=network.target

    [Service]
    Type=simple
    User=YOUR_USERNAME
    Group=YOUR_USERNAME
    WorkingDirectory=/home/YOUR_USERNAME/bamdude
    Environment="PATH=/home/YOUR_USERNAME/bamdude/venv/bin"
    ExecStartPre=-/usr/bin/pkill -9 ffmpeg
    ExecStopPost=-/usr/bin/pkill -9 ffmpeg
    ExecStart=/home/YOUR_USERNAME/bamdude/venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
    Restart=always
    RestartSec=10

    [Install]
    WantedBy=multi-user.target
    ```

    Enable and start:

    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable bamdude
    sudo systemctl start bamdude
    ```

---

## :material-network: Network Requirements

| Port | Protocol | Direction | Purpose |
|------|----------|-----------|---------|
| 8000 | HTTP | Inbound | BamDude web interface |
| 8883 | MQTT/TLS | Outbound | Printer communication |
| 990 | FTPS | Outbound | File transfers from printer |

---

## :material-folder-cog: Build Frontend from Source

The repository includes pre-built frontend files. To build from source:

```bash
cd frontend
npm install
npm run build
cd ..
```

---

## :checkered_flag: Next Steps

<div class="quick-start" markdown>

[:material-printer-3d: **Add Your Printer**<br><small>Connect your first printer</small>](first-printer.md)

[:material-docker: **Try Docker Instead**<br><small>Even simpler setup</small>](docker.md)

[:material-help-circle: **Troubleshooting**<br><small>Installation issues?</small>](../reference/troubleshooting.md)

</div>

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
