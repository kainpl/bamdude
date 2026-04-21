---
title: Swap Mode
description: A1 Mini plate swapper support with swap files and macros
---

# Swap Mode

Swap mode enables support for A1 Mini plate swappers -- automated systems that swap build plates between prints for continuous production without manual intervention.

---

## :material-swap-horizontal: What is Swap Mode?

A plate swapper is a mechanical add-on for the A1 Mini that automatically ejects the finished build plate and positions a fresh one. When swap mode is enabled in BamDude, the queue scheduler coordinates with the swapper to:

1. Run a **swap file** (G-code) after each print completes
2. Trigger **macros** at the right moments
3. Automatically start the next queued print

---

## :material-cog: Configuration

### Enabling Swap Mode

1. Go to **Settings > Queue**
2. Enable **Swap Mode** for your A1 Mini printer
3. Configure the swap file path (G-code file on the printer's SD card)

### Swap File

The swap file is a G-code file that instructs the printer to eject the current plate and prepare for the next print. Typical operations:

- Move the bed forward to the eject position
- Wait for the swap mechanism
- Home axes
- Prepare for the next print

```gcode
; Example swap file
G28 X Y         ; Home X and Y
G1 Y 180 F3000  ; Move bed forward for plate swap
M400            ; Wait for moves to complete
G4 S5           ; Pause 5 seconds for swap
G28             ; Home all axes
```

!!! warning "Custom Hardware Required"
    Swap mode requires a physical plate swapper mechanism attached to your A1 Mini. The swap file must be tailored to your specific hardware.

---

## :material-code-braces: Macros Integration

Swap mode works together with the [macros system](macros.md) to inject G-code at key points:

| Event | When It Fires | Typical Use |
|-------|--------------|-------------|
| **Print End** | After print completes | Trigger plate eject |
| **Print Start** | Before next print begins | Home axes, check plate |

Configure macros in **Settings > Macros** with events specific to swap mode.

---

## :material-playlist-play: Queue Behavior with Swap Mode

When swap mode is active:

1. Print completes on the A1 Mini
2. **Swap file** executes automatically
3. **Plate-clear confirmation is bypassed** -- the swapper handles plate clearing
4. Next queued print starts automatically
5. Cycle repeats until the queue is empty

This enables **unattended batch production** on A1 Mini printers.

---

## :material-alert: Requirements

| Requirement | Details |
|-------------|---------|
| **Printer** | A1 Mini (primary target) |
| **Hardware** | Plate swapper mechanism installed |
| **Swap file** | G-code file on printer SD card |
| **Queue items** | At least 2 prints queued for the printer |

---

## :material-lightbulb: Tips

!!! tip "Test the Swap File"
    Run the swap file manually (from the printer's file browser) before enabling swap mode to verify it works correctly with your hardware.

!!! tip "Combine with Batch Quantity"
    Use the batch quantity feature to queue multiple copies, then let swap mode run them back-to-back automatically.

!!! tip "Monitor Remotely"
    Use the camera streaming and Telegram bot to monitor swap operations remotely and get notified when the queue completes.
