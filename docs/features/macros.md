---
title: Macros
description: G-code and MQTT-action macros triggered by print events
---

# Macros

Macros are small, reusable automations triggered by specific moments in a print's lifecycle. BamDude supports two kinds of macros: classic **G-code macros** (the printer runs custom G-code) and **MQTT-action macros** (BamDude tells the printer to do something via its MQTT control channel — toggle the chamber light, etc.).

Use them for plate-swap mechanisms, chamber lighting, enclosure control, or any other per-event automation that doesn't belong in your slicer's start/end G-code.

---

## :material-code-braces: Action Types

A macro's `action_type` decides what happens when its event fires.

=== "G-code"

    Sends a G-code snippet over MQTT (`gcode_line`), wrapped in
    `M1002 gcode_claim_action` markers. The printer ACKs the snippet and
    BamDude waits for the printer's `stg_cur` to return to idle before
    reporting the macro complete.

    **Use cases**

    - Bed levelling, custom park positions, vibration calibration
    - Plate swap-mode operations (eject + home + prep next plate)
    - Anything you would put in a slicer's start/end G-code but want
      orchestrated by BamDude instead

    **Limitations**

    - G-code on `print_started` *fights* the print itself — the firmware
      is already running its own start sequence. Don't do this unless
      you know exactly why.
    - Long G-code on `swap_mode_change_table` is fine; it runs while the
      printer is idle between plates.

=== "MQTT-action"

    Invokes a named MQTT command from BamDude's catalog instead of
    sending G-code. **Fire-and-forget** — the printer doesn't ACK,
    so BamDude doesn't wait for completion.

    **Currently shipping commands**

    | ID | Effect |
    |----|--------|
    | `chamber_light_on` | Turn the chamber light on |
    | `chamber_light_off` | Turn the chamber light off |

    **Use cases**

    - Auto-light-on at print start, auto-light-off at print finish
    - External automations that don't need the G-code pipeline

    The catalog lives in `core/mqtt_macro_actions.py` and is exposed
    to the frontend via `/macros/meta`. New named commands are added
    there as use cases come up.

!!! info "Compatibility"
    Pre-0.4.0 G-code macros keep working unchanged. New macros default
    to G-code unless you explicitly pick **MQTT-action** in the editor.

---

## :material-lightning-bolt: Events

Each macro is bound to exactly one event. Both action types support every event below, but practical fits differ.

| Event | When It Fires | Good Fit |
|-------|--------------|----------|
| **`print_started`** | When `gcode_state` transitions to `RUNNING` (via `on_print_start`) | MQTT-action (e.g. `chamber_light_on`). G-code is dangerous here. |
| **`print_finished`** | When the print reaches a terminal state (`FINISH`, `FAILED`, or `IDLE`-aborted), via `on_print_complete` | Symmetrical to `print_started` — pair with MQTT-action for clean lights-off automation. |
| **`swap_mode_start`** | Before the print starts, when dispatch knows it's running with a swap-mode profile | G-code that prepares the swap mechanism. |
| **`swap_mode_change_table`** | After the print completes, before the queue picks up the next item | G-code that physically swaps plates. See [Swap Mode](swap-mode.md). |

!!! tip "`print_finished` covers all terminal states"
    Whether the print finished cleanly, failed, or was aborted from the
    printer's screen, `print_finished` fires once. Use it when you
    want behaviour to apply regardless of outcome (turn the light off,
    notify external systems, etc.).

---

## :material-filter: Filter Fields

Both action types share the same filter set. A macro fires only when **every** filter matches.

| Field | Behaviour |
|-------|-----------|
| `enabled` | Global toggle. Disabled macros are skipped without further evaluation. |
| `printer_models` | JSON array of model codes (e.g. `["A1 Mini", "X1 Carbon"]`) or `["*"]` for all models. |
| `swap_mode_only` | Fire only when the printer has swap mode enabled. Hidden in the UI for non-swap events. |
| `swap_profile` | Fire only when the printer's selected swap profile matches this value (e.g. `a1mini_v1`). Lets multiple swap-mode G-code variants coexist. |
| `delay_seconds` | 0–3600. Defer the action by N seconds after the trigger. 0 = immediate. |

### Why `delay_seconds` matters

Some events fire *just* before the printer is in the visible state you'd
expect. Chamber-light-on at `print_started` looks premature on some
models because the firmware-side start sequence (heat-up, purge) hasn't
finished. A 10–30 s delay avoids flicker without you having to wire
up your own state-machine.

---

## :material-pencil: Editing Macros

1. Go to **Settings → Macros**.
2. Pick or create a macro. Choose the **Action Type** (G-code or MQTT-action).
3. Pick the **Event**.
4. Set filter fields (printer models, swap mode, profile, delay).
5. For G-code macros, type the snippet into the editor. For MQTT-action macros, pick the command from the dropdown.
6. Save.

!!! warning "Test G-code on the printer first"
    Bad G-code can damage your printer. Run new snippets manually
    before binding them to an event.

---

## :material-cog-play: How Macros Run

Macros run as **fire-and-forget asyncio tasks**. A slow G-code send, a long delay, or even a network blip never blocks the surrounding orchestration — `on_print_start` returns immediately and the macro fires in the background.

For G-code macros, the dispatcher additionally waits for the `on_macro_complete` callback (printer's `stg_cur` returning to idle) before proceeding to the *next* G-code macro in the same event chain. MQTT-action macros are fully fire-and-forget; nothing waits on them.

---

## :material-history: Replacing the Old `auto_light_off` Flag

Pre-0.4.0 BamDude had an `auto_light_off` boolean on each printer. It was dropped in migration `m021` because macros do the same job better — with delay control, on/off symmetry, per-model targeting, and per-swap-profile filters.

To restore the old behaviour:

=== "Lights off when a print finishes"

    | Field | Value |
    |-------|-------|
    | Action type | MQTT-action |
    | Event | `print_finished` |
    | Command | `chamber_light_off` |
    | `delay_seconds` | `0` |

=== "Lights on when a print starts"

    | Field | Value |
    |-------|-------|
    | Action type | MQTT-action |
    | Event | `print_started` |
    | Command | `chamber_light_on` |
    | `delay_seconds` | `10` (let heat-up finish first) |

Pair both for full automatic-lighting cycles. Add `swap_mode_only=true` if you only want lights to cycle for swap-mode runs.

---

## :material-swap-horizontal: Swap Mode Integration

Swap-mode prints rely on G-code macros bound to `swap_mode_start` and `swap_mode_change_table`. See [Swap Mode](swap-mode.md) for the full lifecycle, restart-resilient event tracking, and dispatch ordering on multi-printer farms.

---

## :material-lightbulb: Tips

!!! tip "Pair `print_started` with `print_finished`"
    Use one MQTT-action macro on each event for clean symmetric
    automation (lights, fans, external relays).

!!! tip "Use `delay_seconds` for chamber-light-on"
    A 10–30 s delay hides the heat-up phase from the chamber camera
    and avoids the "premature lights-on" look on H2/X1.

!!! tip "Combine with smart plugs"
    For full plate-eject + cooldown + power-off cycles, pair end-event
    macros with [Smart Plugs](smart-plugs.md) auto power-off.
