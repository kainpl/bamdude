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

### Second printer waits a few seconds before starting

Not a bug. Since 0.4.1, BamDude's dispatch loop runs strictly one job at a time across the whole farm to avoid `database is locked` errors when two prints land on the printer in parallel. The second printer's FTP upload starts as soon as the first job's start command returns -- typically a few seconds. See [Staggered Start > Dispatch is always serialized](../features/staggered-start.md#material-shield-check-dispatch-is-always-serialized).

---

## :material-cursor-pointer: Skip-Objects Button Issues

### Skip-objects button is greyed out for OrcaSlicer files

OrcaSlicer ships with both `Label objects` and `Exclude objects` **off** in the print profile, so its 3MFs land in BamDude without the metadata the firmware needs to address individual objects. Bambu Studio enables both by default, so files sliced there work out of the box.

**Fix per file:**

1. **Print Settings > Others > "Label objects"** -- emits the per-object IDs (`M624`/`M625`) the firmware needs.
2. **Print Settings > Others > "Exclude objects"** -- turns on the slicer-side metadata BamDude reads.
3. Both must be ticked **before slicing**. Re-sending an old 3MF without these flags doesn't help -- re-slice with both on, re-upload, and the button lights up.

The flags are stored in the source 3MF's `Metadata/project_settings.config`; BamDude extracts them on upload (and backfills existing files via migration `m022`).

### Skip-objects button "dies" 5 minutes into a print (old behaviour)

Fixed in 0.4.1, no action needed. Earlier versions periodically swapped the printer's MQTT client for a fresh instance every ~5 minutes, which wiped the in-memory `printable_objects` state. The button is now repopulated whenever the duplicate-guard branch of `on_print_start` re-fires, so it stays alive for the whole print. Restart-resilient as well -- printer-started fallback prints get the same treatment via `archive_download_retry`.

---

## :material-arrow-up-bold-circle: Upgrade-Time Hangs

### "Server takes minutes to come up" on first boot after 0.4.1

Migration `m022` opens every existing 3MF on disk to backfill two new metadata fields. Roughly 50-200 ms per file. An install with thousands of archives can spend several minutes inside the migration step before the API responds. Watch for `m022 library_files: progress` and `m022 print_archives: progress` log lines -- if you see them advancing in batches of 100, the migration is healthy and just needs to finish.

This is one-shot -- subsequent boots skip the migration via the `_migrations` table.

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
