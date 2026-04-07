---
title: Prometheus Metrics
description: Export printer telemetry for Grafana dashboards
---

# Prometheus Metrics

BamDude can expose printer telemetry in Prometheus format for integration with **Grafana**, **Prometheus**, and other monitoring systems.

---

## :material-cog: Configuration

Navigate to **Settings > Network > Prometheus Metrics**.

| Setting | Description | Default |
|---------|-------------|---------|
| **Enable Metrics** | Toggle endpoint on/off | Off |
| **Bearer Token** | Optional authentication | Empty |

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

| Metric | Type | Description |
|--------|------|-------------|
| `bamdude_printer_connected` | gauge | Connection status (1/0) |
| `bamdude_printer_state` | gauge | Printer state (idle, running, etc.) |
| `bamdude_print_progress` | gauge | Current print progress (0-100%) |
| `bamdude_print_remaining_seconds` | gauge | Estimated remaining time |
| `bamdude_nozzle_temp` | gauge | Nozzle temperature |
| `bamdude_bed_temp` | gauge | Bed temperature |
| `bamdude_chamber_temp` | gauge | Chamber temperature |

---

## :material-chart-bar: Grafana Dashboard

Add BamDude as a Prometheus data source in Grafana to create dashboards with printer telemetry, print progress, temperature trends, and fleet utilization.

---

## :material-lightbulb: Tips

!!! tip "Scrape Interval"
    A 15-30 second scrape interval is sufficient for printer telemetry.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
