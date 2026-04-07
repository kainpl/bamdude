---
title: Macros
description: G-code macros triggered by print events
---

# Macros

BamDude's macro system lets you inject custom G-code at key points during print execution, enabling automation for plate swappers, enclosure control, and custom workflows.

---

## :material-code-braces: What Are Macros?

Macros are G-code snippets that are automatically injected into print files when dispatched from the queue. They run at specific events during the print lifecycle.

---

## :material-lightning-bolt: Events

Macros can be triggered by these events:

| Event | When It Fires | Example Use |
|-------|--------------|-------------|
| **Print Start** | Before the first line of G-code | Home axes, check plate, start enclosure fan |
| **Print End** | After the last line of G-code | Eject plate, power off heaters, trigger swap mechanism |

---

## :material-pencil: Macro Editor

### Creating a Macro

1. Go to **Settings > Macros** (or **Settings > Workflow > G-code Injection**)
2. Select the target printer model
3. Enter G-code for start and/or end events
4. Changes save automatically

### G-code Editor

The built-in editor provides:

- **Syntax highlighting** for G-code
- **Line numbers** for reference
- **Per-printer model** configuration -- different macros for X1C vs A1 Mini
- **Live preview** of the injection points

---

## :material-printer: Per-Printer Model Configuration

Macros are configured per printer model. Only models you have connected appear in the settings.

| Printer Model | Start G-code | End G-code |
|---------------|:------------:|:----------:|
| A1 Mini | Custom | Custom |
| X1 Carbon | Custom | Custom |
| P1S | Custom | Custom |

This allows different automation for different printer types.

---

## :material-playlist-play: How Injection Works

When the scheduler dispatches a print with macros enabled:

1. Looks up G-code snippets for the target printer's model
2. Creates a **temporary copy** of the 3MF with snippets injected
3. Uploads the modified copy via FTP
4. Cleans up the temporary file after upload

!!! info "Original Files Unchanged"
    Injection never modifies your archive or library files. A temporary copy is created for upload only.

---

## :material-toggle-switch: Enabling Per Queue Item

1. Open the **Add to Queue** or **Edit Queue Item** dialog
2. Check **Inject auto-print G-code**
3. Submit -- the queue item shows a green **G-code** badge

---

## :material-swap-horizontal: Swap Mode Integration

Macros are a key component of [swap mode](swap-mode.md). The end G-code macro handles plate ejection, and the start G-code ensures the new plate is positioned correctly.

---

## :material-lightbulb: Tips

!!! tip "Test First"
    Always test your G-code manually on the printer before using it in macros. Bad G-code can damage your printer.

!!! tip "Keep It Simple"
    Macros run synchronously. Long-running G-code (like extensive heating) will delay the next print.

!!! tip "Combine with Smart Plugs"
    Use end macros to trigger plate eject, then smart plug auto-off after cooldown for fully automated workflows.
