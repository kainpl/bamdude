# BamDude Installation Scripts

Interactive installation scripts for BamDude with support for both native and Docker deployments.

## Quick Start

### Docker Installation (Recommended)

**Linux/macOS:**
```bash
curl -fsSL https://raw.githubusercontent.com/kainpl/bamdude/main/install/docker-install.sh -o docker-install.sh && chmod +x docker-install.sh && ./docker-install.sh
```

### Native Installation

**Linux/macOS:**
```bash
curl -fsSL https://raw.githubusercontent.com/kainpl/bamdude/main/install/install.sh -o install.sh && chmod +x install.sh && ./install.sh
```

---

## Scripts Overview

| Script | Platform | Method |
|--------|----------|--------|
| `install.sh` | Linux, macOS | Native (Python venv) |
| `docker-install.sh` | Linux, macOS | Docker |
| `update.sh` | Linux (systemd) | Native update helper |

---

## Native Installation Scripts

### `install.sh` (Linux/macOS)

Installs BamDude with Python virtual environment and optional systemd/launchd service.

**Supported Systems:**
- Debian/Ubuntu (apt)
- RHEL/Fedora/CentOS (dnf/yum)
- Arch Linux (pacman)
- openSUSE (zypper)
- macOS (Homebrew)

**Options:**
```
--path PATH        Installation directory (default: /opt/bamdude)
--port PORT        Port to listen on (default: 8000)
--tz TIMEZONE      Timezone (default: system timezone)
--data-dir PATH    Data directory (default: INSTALL_PATH/data)
--log-dir PATH     Log directory (default: INSTALL_PATH/logs)
--debug            Enable debug mode
--log-level LEVEL  Log level: DEBUG, INFO, WARNING, ERROR (default: INFO)
--no-service       Skip systemd/launchd service setup
--yes, -y          Non-interactive mode, accept defaults
```

**Examples:**
```bash
# Interactive installation
./install.sh

# Unattended with custom settings
./install.sh --path /srv/bamdude --port 3000 --tz America/New_York --yes

# Minimal unattended
./install.sh -y

# Skip service setup
./install.sh --no-service -y
```

---

## Docker Installation Scripts

### `docker-install.sh` (Linux/macOS)

Installs BamDude using Docker containers.

**Options:**
```
--path PATH        Installation directory (default: ~/bamdude)
--port PORT        Port to expose (default: 8000)
--tz TIMEZONE      Timezone (default: system timezone)
--build            Build from source instead of using pre-built image
--yes, -y          Non-interactive mode, accept defaults
```

**Examples:**
```bash
# Interactive installation
./docker-install.sh

# Unattended with custom settings
./docker-install.sh --path /srv/bamdude --port 3000 --tz Europe/Berlin --yes

# Build from source
./docker-install.sh --build --yes
```

---

## Configuration Options

All scripts support these configuration options:

| Option | Description | Default |
|--------|-------------|---------|
| Install Path | Where BamDude is installed | `/opt/bamdude` (Linux/Docker) |
| Port | HTTP port for web interface | `8000` |
| Timezone | Server timezone | System timezone or `UTC` |
| Data Directory | Database and archives | `INSTALL_PATH/data` |
| Log Directory | Application logs | `INSTALL_PATH/logs` |
| Debug Mode | Enable verbose logging | `false` |
| Log Level | INFO, WARNING, ERROR, DEBUG | `INFO` |

---

## Post-Installation

### Accessing BamDude

After installation, open your browser to:
```
http://localhost:8000
```

Or use the port you specified during installation.

### Service Management

**Linux (systemd):**
```bash
sudo systemctl status bamdude    # Check status
sudo systemctl start bamdude     # Start
sudo systemctl stop bamdude      # Stop
sudo systemctl restart bamdude   # Restart
sudo journalctl -u bamdude -f    # View logs
```

**macOS (launchd):**
```bash
launchctl list | grep bamdude                              # Check status
launchctl load ~/Library/LaunchAgents/com.bamdude.app.plist    # Start
launchctl unload ~/Library/LaunchAgents/com.bamdude.app.plist  # Stop
```

**Docker:**
```bash
docker compose ps           # Check status
docker compose up -d        # Start
docker compose down         # Stop
docker compose restart      # Restart
docker compose logs -f      # View logs
```

### Updating

**Native installation:**
```bash
curl -fsSL https://raw.githubusercontent.com/kainpl/bamdude/main/install/update.sh -o update.sh
chmod +x update.sh
sudo ./update.sh
```

The updater performs:
- Root permission check (fails fast before any work)
- Optional built-in backup API call (`/api/v1/settings/backup`) before update
- Keeps only the newest 5 local backup ZIP files
- Local-change warning + confirmation before `git reset --hard`
- If remote has no new commits, updater exits early without stopping the service
- Service stop/start with code rollback + service restart attempt if update fails

Useful environment overrides:
```bash
# Typical native install defaults
INSTALL_DIR=/opt/bamdude SERVICE_NAME=bamdude sudo ./update.sh

# Require backup to succeed (abort update if backup fails)
BACKUP_MODE=require sudo ./update.sh

# Skip backup API call
BACKUP_MODE=skip sudo ./update.sh

# Auth-enabled instances: provide API key for backup endpoint
BAMDUDE_API_KEY=bb_xxx BACKUP_MODE=require sudo ./update.sh
```

**Docker (pre-built image):**
```bash
cd ~/bamdude
docker compose pull
docker compose up -d
```

**Docker (from source):**
```bash
cd ~/bamdude
git pull
docker compose up -d --build
```

---

## Troubleshooting

### Permission Denied (Linux)
Run with `sudo` or ensure your user has appropriate permissions:
```bash
sudo ./install.sh
```

### Docker: Printer Discovery Not Working
Docker Desktop for macOS doesn't support host networking. Add printers manually by IP address in the BamDude web interface.

### Service Won't Start
Check logs for errors:
```bash
# Linux
sudo journalctl -u bamdude -n 50

# Docker
docker compose logs bamdude
```

### Port Already in Use
Choose a different port during installation or stop the conflicting service:
```bash
# Find what's using port 8000
sudo lsof -i :8000  # Linux/macOS
```

---

## Requirements

### Native Installation
- Python 3.10+ (automatically installed if missing)
- Node.js 18+ (automatically installed if missing)
- Git (automatically installed if missing)
- ~500MB disk space

### Docker Installation
- Docker Engine 20+ or Docker Desktop
- ~1GB disk space (includes image)

---

## Support

- **Documentation:** https://docs.bamdude.top
- **Discord:** https://discord.gg/aFS3ZfScHM
- **Issues:** https://github.com/kainpl/bamdude/issues
