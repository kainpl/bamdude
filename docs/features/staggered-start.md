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

## :material-shield-check: Dispatch is always serialized

Even with stagger disabled, BamDude's dispatch loop processes one job at a time across the whole farm. This is a 0.4.1 reliability change -- back-to-back dispatches to two different printers used to race on `INSERT INTO print_archives` and SQLite's single-writer semantics could fail the second job mid-FTP with `database is locked`. Now jobs run strictly one-at-a-time.

What this means in practice:

- **Without stagger**, the second printer waits a few seconds (until the first job's FTP upload + start command finish) before its own FTP upload starts. Not a bug -- a deliberate trade-off for archive integrity.
- **Stagger is still useful** for spreading the *running-print* heat load -- bed currents drawn while the prints are physically running, not the FTP/MQTT-startup moment that serialization already handles. A 6-printer batch with stagger off still has all 6 beds heating within a few seconds of each other once each FTP completes.

Use stagger when peak power draw during heating is the constraint; ignore stagger if your circuit can handle simultaneous bed heating and you only care about avoiding the SQLite race (already handled).

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
