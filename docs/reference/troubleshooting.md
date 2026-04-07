---
title: Troubleshooting
description: Common issues and solutions
---

# Troubleshooting

Solutions for common issues with BamDude.

---

## :material-printer-3d: Printer Connection Issues

### Printer Won't Connect

**Symptoms:** Printer shows as disconnected, red indicator.

**Solutions:**

1. **Verify Developer Mode is enabled**
   - Settings > Network > LAN Only Mode (ON)
   - Then enable Developer Mode
   - Toggle off/on to get a fresh access code

2. **Check IP address**
   - Verify IP in printer network settings
   - Use static IP or DHCP reservation

3. **Verify access code**
   - Access code changes when Developer Mode is toggled
   - Copy the code exactly (case-sensitive)

4. **Check network connectivity**
   ```bash
   ping YOUR_PRINTER_IP
   ```

5. **Verify ports are accessible**
   - MQTT: Port 8883
   - FTPS: Port 990

6. **Check firewall rules**

---

### Connection Drops Frequently

1. **Check WiFi signal strength** on the printer card
2. **Network congestion** -- Try a dedicated network/VLAN
3. **Router issues** -- Restart, check firmware, disable "smart" features
4. **Check BamDude logs:**
   ```bash
   tail -f logs/bamdude.log
   ```

---

## :material-camera: Camera Issues

### Stream Won't Start

1. Is the printer powered on?
2. Is camera enabled in printer settings?
3. Is ffmpeg installed? (included in Docker image)
4. Is Developer Mode enabled?
5. Docker users: try `network_mode: host`

### Stream Freezes

- Check WiFi signal strength
- Try lowering FPS
- Use snapshot mode instead

---

## :material-archive: Archiving Issues

### Prints Not Being Archived

1. **SD card inserted?** Required for file downloads
2. **Developer Mode enabled?** Required for FTP access
3. **Auto-archive enabled?** Check per-printer setting
4. **Calibration prints** are automatically skipped

---

## :material-clock-outline: Queue Issues

### Prints Not Starting

1. **Printer connected?** Must show green indicator
2. **Plate cleared?** Check if "Clear Plate & Start Next" button is showing
3. **Scheduled time?** Check if print has a future schedule
4. **Queue Only mode?** Check for purple "Staged" badge

---

## :material-docker: Docker Issues

### Container Won't Start

```bash
docker compose logs bamdude
```

### Can't Connect to Printer

```bash
docker compose exec bamdude ping YOUR_PRINTER_IP
```

Try `network_mode: host` on Linux.

### macOS / Windows Docker

Docker Desktop runs containers in a VM. Use port mapping instead of host mode, and add printers manually by IP.

---

## :material-send: Telegram Bot Issues

### Bot Not Responding

1. Check that the Telegram provider is enabled in Settings > Notifications
2. Verify the bot token is correct
3. Check BamDude logs for polling errors
4. Ensure your chat is authorized

### Commands Not Working

1. Check that your chat has the required permissions
2. Verify the chat's group assignment in the web UI
3. Try `/start` to re-register the chat

---

## :material-database: Database Issues

### Resetting the Database

!!! danger "Data Loss"
    This deletes all your print history and settings!

```bash
docker compose down
# Remove the database file from the data volume
docker compose up -d
```

---

## :material-bug: Getting Help

When reporting issues, include:

- BamDude version
- Printer model and firmware version
- Operating system
- Steps to reproduce
- Error messages from logs
- Docker compose configuration (if applicable)

File issues at [github.com/kainpl/bamdude/issues](https://github.com/kainpl/bamdude/issues).

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
