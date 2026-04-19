---
title: Installation
description: Install BamDude on your system
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

| Variable | Default | Description |
|----------|---------|-------------|
| `DEBUG` | `false` | Enable debug mode (verbose logging) |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_TO_FILE` | `true` | Write logs to `logs/bamdude.log` |

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
