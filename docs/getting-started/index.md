---
title: Getting Started
description: Get up and running with BamDude in minutes
---

# Getting Started

Welcome to BamDude! This guide will help you get your print farm management system up and running quickly.

---

## :rocket: Quick Install

=== ":material-docker: Docker (Recommended)"

    ```bash
    git clone https://github.com/kainpl/bamdude.git
    cd bamdude
    docker compose up -d
    ```

    Open [http://localhost:8000](http://localhost:8000) in your browser.

    [:material-arrow-right: Full Docker Guide](docker.md)

=== ":material-language-python: Python"

    ```bash
    git clone https://github.com/kainpl/bamdude.git
    cd bamdude
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
    ```

    Open [http://localhost:8000](http://localhost:8000) in your browser.

    [:material-arrow-right: Full Installation Guide](installation.md)

---

## :footprints: Next Steps

<div class="feature-grid" markdown>

<div class="feature-card" markdown>
### :material-numeric-1-circle: Enable Developer Mode
Enable Developer Mode on your printer and note the access code.

[:material-arrow-right: See instructions](#enabling-developer-mode)
</div>

<div class="feature-card" markdown>
### :material-numeric-2-circle: Add Your Printer
Enter your printer's IP, access code, and serial number.

[:material-arrow-right: Add first printer](first-printer.md)
</div>

<div class="feature-card" markdown>
### :material-numeric-3-circle: Start Printing!
BamDude automatically archives every print and manages your queue.

[:material-arrow-right: Explore features](../features/index.md)
</div>

</div>

---

## Enabling Developer Mode

BamDude connects to your printer via **Developer Mode** -- a local connection that provides full control without internet.

!!! info "Why Developer Mode?"
    Developer Mode provides direct communication between BamDude and your printer over your local network:

    - :material-check: **Works offline** -- No internet required
    - :material-check: **Full control** -- Start/stop prints, upload files, control lights
    - :material-check: **Your data stays local** -- No cloud dependency

!!! warning "Developer Mode vs LAN Only Mode"
    Since the January 2025 firmware update, standard LAN Only Mode (without Developer Mode) only provides **read-only** access. **Developer Mode is required** for full functionality with BamDude.

### Step 1: Enable LAN Only Mode

1. On your printer's touchscreen, go to **Settings**
2. Navigate to **Network** or **WLAN**
3. Toggle **LAN Only Mode** to **ON**

### Step 2: Enable Developer Mode

1. After enabling LAN Only Mode, a **Developer Mode** option will appear
2. Toggle **Developer Mode** to **ON**
3. Note down the **Access Code** displayed (8 characters)

!!! warning "Access Code Changes"
    The access code changes every time you toggle these modes off and on. If you re-enable Developer Mode, you'll need to update the access code in BamDude.

### Step 3: Insert SD Card

!!! warning "SD Card Required"
    An SD card must be inserted in your printer for BamDude to work properly. The SD card is required for file transfers, starting prints, and archiving completed prints.

### Step 4: Gather Printer Information

You'll need these details to add your printer:

| Information | Where to Find |
|------------|---------------|
| **IP Address** | Settings :material-arrow-right: Network |
| **Serial Number** | Settings :material-arrow-right: Device Info |
| **Access Code** | Shown when Developer Mode is enabled |

---

## :checkered_flag: What's Next?

<div class="quick-start" markdown>

[:material-printer-3d: **Add Your Printer**<br><small>Connect your first printer</small>](first-printer.md)

[:material-archive: **Print Archiving**<br><small>How automatic archiving works</small>](../features/archiving.md)

[:material-bell-ring: **Notifications**<br><small>Get alerts on your phone</small>](../features/notifications.md)

[:material-help-circle: **Troubleshooting**<br><small>Having issues?</small>](../reference/troubleshooting.md)

</div>

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
