---
title: Staggered Start
description: Roll out batch prints in groups to avoid power spikes
---

# Staggered Start

When sending prints to multiple printers simultaneously, staggered start prevents power spikes from concurrent bed heating by rolling out starts in configurable groups.

---

## :material-timer-sand: Why Stagger?

Starting 10+ printers at the same time can cause significant power draw as all beds heat simultaneously. Staggered start distributes the load by starting printers in groups with intervals between them.

---

## :material-cog: How It Works

1. Select multiple printers in the **Print** or **Add to Queue** dialog
2. Enable **Stagger printer starts** (appears automatically with multiple printers)
3. Configure:

| Setting | Description | Default |
|---------|-------------|---------|
| **Group size** | How many printers start at once | 2 |
| **Interval** | Minutes between each group starting | 5 min |

4. A preview shows the schedule, e.g., "6 printers -> 3 groups of 2, starting every 5 min (total: 10 min)"

---

## :material-play-circle: Execution

- **First group** starts immediately (or at the scheduled time)
- **Subsequent groups** start at computed intervals
- The scheduler uses `scheduled_time` on queue items -- no special logic needed

### Example

With 6 printers, group size 2, interval 5 minutes:

| Time | Action |
|------|--------|
| 0 min | Printers A and B start |
| 5 min | Printers C and D start |
| 10 min | Printers E and F start |

---

## :material-tune: Default Settings

Configure default stagger values in **Settings > Queue > Staggered Start**. These can be overridden per batch in the Print or Schedule dialog.

---

## :material-monitor-dashboard: Bed Temperature Monitoring

BamDude monitors bed temperatures across all printers. Combined with staggered start, this helps:

- **Avoid circuit overloads** from simultaneous heating
- **Spread power draw** across time
- **Monitor heat-up times** per printer

---

## :material-lightbulb: Tips

!!! tip "Power Management"
    Combine staggered starts with smart plug auto-off for full power management: stagger prevents peak draw at start, auto-off cuts idle power at finish.

!!! tip "Interval Tuning"
    Set the interval to the time your printers take to reach bed temperature (usually 2-5 minutes). This ensures each group has finished heating before the next starts.

!!! tip "Per-Printer Intervals"
    For mixed farms with different printer models, use a longer interval to account for the slowest heater.
