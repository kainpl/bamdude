---
title: Prometheus Metrics
description: Export printer telemetry for Grafana dashboards
---

# Prometheus Metrics

BamDude can expose printer telemetry in Prometheus format for integration with **Grafana**, **Prometheus**, and other monitoring systems.

---

## :material-cog: Configuration

Navigate to **Settings > Network > Prometheus Metrics**.

| Setting | DB key | Description | Default |
|---------|--------|-------------|---------|
| **Enable Metrics** | `prometheus_enabled` | Toggle endpoint on/off | Off |
| **Bearer Token** | `prometheus_token` | Optional Bearer-token auth on `/metrics` | Empty (open) |

!!! info "Auth on /metrics"
    `/api/v1/metrics` ignores BamDude's normal auth stack -- it has its own gate. When `prometheus_enabled=false` it returns 404 (looks unconfigured). When enabled with no `prometheus_token`, it's open. When enabled with a token, callers must send `Authorization: Bearer <token>`. Set the token whenever Prometheus runs on a separate host you don't fully trust.

---

## :material-api: Endpoint

```
GET /api/v1/metrics
```

Returns metrics in [Prometheus text exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/).

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" http://bamdude:8000/api/v1/metrics
```

---

## :material-chart-line: Available Metrics

Each per-printer metric is labelled with `printer_id`, `printer_name`, and `serial`. Aggregate counters/gauges are unlabelled or labelled by `result` / `fan` / `nozzle` as appropriate.

### Build info

| Metric | Type | Description |
|--------|------|-------------|
| `bamdude_build_info` | gauge | `version`, `python_version`, `platform`, `architecture` (always = 1) |

### Per-printer state

| Metric | Type | Description |
|--------|------|-------------|
| `bamdude_printer_connected` | gauge | Connection status (1/0) |
| `bamdude_printer_state` | gauge | 0=unknown, 1=idle, 2=running, 3=pause, 4=finish, 5=failed, 6=prepare, 7=slicing |
| `bamdude_print_progress` | gauge | Current print progress (0-100) |
| `bamdude_print_remaining_seconds` | gauge | Estimated remaining time (seconds) |
| `bamdude_print_layer_current` | gauge | Current layer number |
| `bamdude_print_layer_total` | gauge | Total layers in current print |

### Temperatures + fans

| Metric | Type | Description |
|--------|------|-------------|
| `bamdude_bed_temp_celsius` | gauge | Current bed temperature |
| `bamdude_bed_target_celsius` | gauge | Target bed temperature |
| `bamdude_nozzle_temp_celsius` | gauge | Current nozzle temperature (label `nozzle="0"`/`"1"` for H2D dual-nozzle) |
| `bamdude_nozzle_target_celsius` | gauge | Target nozzle temperature |
| `bamdude_chamber_temp_celsius` | gauge | Chamber temperature (only emitted for models with the sensor) |
| `bamdude_fan_speed_percent` | gauge | Fan speed (label `fan="part"`/`"aux"`/`"chamber"`) |
| `bamdude_wifi_signal_dbm` | gauge | WiFi signal strength in dBm |

### Aggregate (DB-derived)

| Metric | Type | Description |
|--------|------|-------------|
| `bamdude_prints_total` | counter | Lifetime print count, label `result="completed"`/`"failed"`/etc. |
| `bamdude_printer_prints_total` | counter | Lifetime print count per printer |
| `bamdude_filament_used_grams` | counter | Total filament consumed |
| `bamdude_print_time_seconds` | counter | Total print time logged |
| `bamdude_queue_pending` | gauge | Number of pending queue items |
| `bamdude_queue_printing` | gauge | Number of currently printing queue items |
| `bamdude_printers_connected` | gauge | Connected printers right now |
| `bamdude_printers_total` | gauge | Configured printers right now |

---

## :material-chart-bar: Grafana Dashboard

Add BamDude as a Prometheus data source in Grafana to create dashboards with printer telemetry, print progress, temperature trends, and fleet utilization.

---

## :material-lightbulb: Tips

!!! tip "Scrape Interval"
    A 15-30 second scrape interval is sufficient for printer telemetry.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
