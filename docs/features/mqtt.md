---
title: MQTT Publishing
description: Publish events to external MQTT brokers
---

# MQTT Publishing

BamDude can publish events to an external MQTT broker, enabling integration with **Home Assistant**, **Node-RED**, and other MQTT-based systems.

!!! info "Three different MQTT roles"
    BamDude touches MQTT in three independent places:

    1. **MQTT relay (this page)** -- BamDude *publishes* its own state to your external broker so HA / Node-RED can subscribe.
    2. **Printer-side MQTT** -- BamDude *connects to each printer's* internal MQTT broker (Bambu's protocol) to receive `push_status` and send commands. Configured per-printer when you add the printer; invisible to operators after that.
    3. **Smart-plug MQTT subscriber** -- a separate code path subscribes BamDude *to your broker* to receive smart-plug telemetry (Tasmota / Zigbee2MQTT / Sonoff). Configured per-plug under **Settings > Smart Plugs**.

    This page only covers the relay (#1).

---

## :material-cog: Configuration

Navigate to **Settings > Network > MQTT Publishing**.

| Setting | Description | Default |
|---------|-------------|---------|
| **Enable MQTT** | Toggle publishing on/off | Off |
| **Broker Hostname** | MQTT broker address | -- |
| **Port** | Broker port | 1883 (8883 with TLS) |
| **Username** | Authentication (optional) | -- |
| **Password** | Authentication (optional) | -- |
| **Topic Prefix** | Prefix for all topics | `bambuddy` (legacy default — change to `bamdude` if starting fresh) |
| **Use TLS** | Enable TLS/SSL encryption | Off |

---

## :material-broadcast: Published Topics

All topics are prefixed with your configured prefix (default: `bamdude`).

### Printer Events

| Topic | Description |
|-------|-------------|
| `bamdude/printers/{serial}/status` | Real-time printer state (throttled) |
| `bamdude/printers/{serial}/print/started` | Print job started |
| `bamdude/printers/{serial}/print/completed` | Print completed |
| `bamdude/printers/{serial}/print/failed` | Print failed |
| `bamdude/printers/{serial}/ams/changed` | AMS filament changed |

### Queue Events

| Topic | Description |
|-------|-------------|
| `bamdude/queue/added` | Job added to queue |
| `bamdude/queue/started` | Job started printing |
| `bamdude/queue/completed` | Job completed |

---

## :material-home-assistant: Home Assistant Example

```yaml
mqtt:
  sensor:
    - name: "Printer Status"
      state_topic: "bamdude/printers/YOUR_SERIAL/status"
      value_template: "{{ value_json.state }}"
```

---

## :material-lightbulb: Tips

!!! tip "Topic Discovery"
    Use MQTT Explorer to browse published topics and understand the payload structure.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
