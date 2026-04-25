---
title: Docker Installation
description: Deploy BamDude with Docker in one command
---

# Docker Installation

Docker is the easiest way to run BamDude. One command and you're done.

---

## :rocket: Quick Start

=== ":material-download: Pre-built Image"

    ```bash
    mkdir bamdude && cd bamdude
    curl -O https://raw.githubusercontent.com/kainpl/bamdude/main/docker-compose.yml
    docker compose up -d
    ```

=== ":material-source-branch: Build from Source"

    ```bash
    git clone https://github.com/kainpl/bamdude.git
    cd bamdude
    docker compose up -d --build
    ```

Open [http://localhost:8000](http://localhost:8000) in your browser.

---

## :material-cog: Configuration

### docker-compose.yml

```yaml
services:
  bamdude:
    image: ghcr.io/kainpl/bamdude:latest
    build: .
    container_name: bamdude
    network_mode: host
    volumes:
      - bamdude_data:/app/data
      - bamdude_logs:/app/logs
    environment:
      - TZ=Europe/Berlin
    restart: unless-stopped

volumes:
  bamdude_data:
  bamdude_logs:
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `UTC` | Your timezone (e.g., `America/New_York`) |
| `PORT` | `8000` | Port BamDude runs on |
| `DEBUG` | `false` | Enable debug logging |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_TO_FILE` | `true` | Write logs to `/app/logs/bamdude.log` |
| `DATABASE_URL` | unset (SQLite) | PostgreSQL URL, e.g. `postgresql+asyncpg://user:pass@host:5432/bamdude` |
| `TRUSTED_PROXY_IPS` | empty | Comma-separated reverse-proxy IPs trusted for `X-Forwarded-For` (set this when fronting BamDude with nginx / Caddy / Traefik) |
| `AUTH_REFRESH_COOKIE_SECURE` | unset (auto) | Force the refresh-cookie `Secure` flag. Auto-detect from request scheme by default. |
| `MFA_ENCRYPTION_KEY` | unset | URL-safe base64 Fernet key for at-rest encryption of TOTP / OIDC secrets. |
| `APP_URL` | `http://localhost:5173` | Public URL of BamDude (used in WebAuthn RP-ID + notification links). |
| `JWT_SECRET_KEY` | auto-generated, persisted | Don't change on a running install -- it invalidates all issued tokens. |

See [Installation > Environment Variables](installation.md#environment-variables) for the full list including optional integrations.

---

## :material-database: Data Persistence

| Volume | Purpose |
|--------|---------|
| `bamdude.db` | SQLite database with all your print data |
| `archive/` | Archived 3MF files and thumbnails |
| `logs/` | Application logs |

!!! tip "Backup"
    To backup your data, simply copy these files/directories. See [Backup & Restore](../features/backup.md) for the built-in backup feature.

---

## :material-update: Updating

=== ":material-download: Pre-built Image"

    ```bash
    docker compose pull && docker compose up -d
    ```

=== ":material-source-branch: Built from Source"

    ```bash
    cd bamdude && git pull && docker compose build --pull && docker compose up -d
    ```

---

## :material-server: Advanced Setups

### Reverse Proxy (Nginx)

```nginx
server {
    listen 443 ssl http2;
    server_name bamdude.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}
```

!!! warning "WebSocket Support"
    Make sure your reverse proxy supports WebSocket connections -- required for real-time printer updates.

### Network Mode Host

Host network mode is **required** for printer discovery and camera streaming on Linux:

```yaml
services:
  bamdude:
    network_mode: host
```

!!! note "macOS / Windows"
    Docker Desktop on macOS and Windows requires port mapping instead of host mode. Use `ports: ["8000:8000"]` and add printers manually by IP.

---

## :material-help-circle: Troubleshooting

### Container Won't Start

```bash
docker compose logs bamdude
```

### Can't Connect to Printer

```bash
docker compose exec bamdude ping YOUR_PRINTER_IP
```

If using bridge network mode, try `network_mode: host`.

---

## :checkered_flag: Next Steps

<div class="quick-start" markdown>

[:material-printer-3d: **Add Your Printer**<br><small>Connect your first printer</small>](first-printer.md)

[:material-arrow-up-circle: **Upgrading**<br><small>Migrate from Bambuddy</small>](upgrading.md)

[:material-help-circle: **Troubleshooting**<br><small>Having issues?</small>](../reference/troubleshooting.md)

</div>

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
