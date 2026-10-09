# Order auto-eject MVP

The order's **Auto-eject / Автоскидання** checkbox applies to newly created
queue and auto-queue jobs. Existing jobs keep their saved value. Copies,
repeats and queue rebalancing preserve that value. Printer-wide settings are
not changed; the feature is not restricted to a printer model.

This is an experimental workflow for operator-prepared files. Successful
ejection of one part does not establish that another part, material, surface
or finish sequence will eject. Camera checks are enabled by default.

## Operator setup

Enabling the order setting opens the existing confirmation dialog. Read the
requirements and explicitly acknowledge the prepared files, installed hardware
and camera calibration before saving. Disabling the setting remains immediate.
The confirmation describes the operator's choice; it does not validate a preset
inside the file or grant model-wide admission.

### Reference profile: A1 mini Tilt Kit

The operator-supplied **A1 mini Tilt Kit End G-code**, attributed to **Infinity
Flow 3D Printing** and dated **2025-10-08**, is the reference for this trial.
Obtain the matching kit, slicer profile and G-code from the
[author's source page](https://infinityflow3d.com/pages/free-3d-printer-auto-clearing-cad-and-g-code).
The date identifies the supplied template, not a claim that it is the latest
download on that page. Use the preset for the installed tilt hardware, not the
stock A1 mini printer preset.

- Its ejection branch runs only when `max_layer_z > 5.5` mm. Shorter jobs skip
  ejection; do not use this template unchanged for those auto-eject jobs.
- After that branch, the commanded finish position is `X5 Y185 Z6`: the last
  sweep is at `X5`, the last bed move is `Y185`, and `Z1` is followed by a
  relative `Z5` lift. The shorter-job branch has a different, variable Z pose.
- Match camera references and ROI to the actual post-ejection pose. The ROI
  must cover the print area and auxiliary parts. Nothing outside the camera
  image or ROI is observable; a clear result only describes the checked area.
- Do not add blind homing before the photo. Successful G-code completion does
  not itself prove ejection; with checking enabled, the fresh photo decides
  whether to proceed. Camera opt-out removes this observation.
- P1S and other printers need their own prepared hardware and finish sequence.
  Never copy the A1 mini motion coordinates to another printer model.

The existing detector compares against the best matching reference. When
changing pose or camera geometry, back up and replace references from the old
setup, rather than retaining unrelated references as alternative empty states.

### Queue and camera setup

1. Select a prepared G-code file whose successful completion includes the
   physical ejection. BamDude does not inspect or modify the ejection sequence.
2. Calibrate the existing empty-plate detector for the configured camera and
   the plate's expected position after completion. Use the existing ROI and
   reference-image controls. Keep lighting and the camera position consistent.
3. Enable auto-eject on the order before creating its jobs. Verify the mode
   badge on each queued job. This setting is incompatible with Swap Mode.
4. By default, a fresh photo is required immediately before dispatch. Occupied plate,
   unavailable camera, missing calibration or changed printer context leaves
   the queued job waiting. Correct the cause using the existing controls.
5. After a successful auto-eject run, the next dispatch may answer that run's
   plate-clear gate through the same photo check. A preceding ordinary,
   failed, cancelled or uncertain run still requires the existing manual
   answer. Turning on the next job's flag cannot bypass that gate.

Starts from the printer screen or another slicer remain ordinary prints, with
the existing reactive detection and manual clearing. The mode belongs to a
BamDude-dispatched run, not to a printer's current global settings.

### Camera policy for new jobs

Under the enabled order checkbox, **Allowed difference from the reference**
sets the existing detector's image-difference threshold (default 1%, range
0.1–10%). It is not a probability that the plate is empty. A higher value can
miss a retained part. First add empty references for different lighting, in
the same finish position and camera geometry (up to five references), then
test with a real retained part inside the ROI. A clear result says nothing
about areas outside the image or ROI.

**Skip OpenCV plate check** requires two warning dialogs and explicit risk
acknowledgement. In this mode no image is captured before that auto-eject
job; BamDude cannot verify ejection. Jobs display a red “no plate check” badge.
This is an operator opt-out, never a fallback when a camera check fails.
Manual holds after ordinary, failed, cancelled or uncertain prints remain;
fresh local telemetry, claim ownership, compatibility and pause/duplicate
protections are still required. This option neither moves the printer nor
adds G-code. Turning auto-eject back on resets this opt-out to checked mode.

Both values are captured into each new job and its archive dispatch intent.
Changing an order, including re-enabling camera checks, leaves existing jobs
unchanged. Copies and repeats retain their original settings. Old jobs use
the original 1% check; migration does not enable opt-out for them.

## Implementation boundaries

This extends the existing dispatcher, queue claim, plate-answer gate and
OpenCV detector. It adds three boolean columns: order setting, queue snapshot
and auto-queue snapshot. Existing rows migrate to `false`. The archive's
existing dispatch intent records the dispatched mode and captured camera policy.
Nullable JSON columns on these same three tables hold that small policy;
missing policy uses the safe original defaults.

No recipe registry, model-wide admission, G-code parser, ejection command,
new scheduler, retry protocol or restart-recovery subsystem is introduced.
Existing Swap Mode operates a different plate-change workflow. OctoPrint
and Klipper provide other printer-control APIs rather than a compatible
replacement for this project's Bambu dispatcher, so they are not added as
dependencies. References: [OctoPrint printer API](https://docs.octoprint.org/en/main/api/printer.html),
[Klipper G-code commands](https://www.klipper3d.org/G-Codes.html).

Fresh checks reject older buffered/coalesced images, wait for the next frame
when the live viewer owns the camera, and do not open a competing reader.
A failed configured external camera cannot substitute a different camera's
frame against its reference. Detection errors explicitly mean unavailable.

## Validation and rollout

Automated checks use synthetic orders, printers and camera frames. They cover
mode snapshots, copies, migration, failed and ordinary predecessor gates,
camera failures, stale telemetry, changing run tokens, reconnects, cancellation
and existing queue/dispatcher regressions. The development deployment uses the
isolated development environment and cannot reach farm printers.

These checks do not establish physical ejection reliability or the detector's
ability to see a particular small part. A supervised hardware trial and
production activation require separate agreement. Keep the first hardware
trial limited to one printer with a prepared file; observe the existing check
with both an empty plate and a retained part before allowing unattended work.

The polygon inspection area is maintained independently in PR #71. When that
feature is available, dispatch uses its saved mask and refuses the result if
the mask changes while the photo is being checked. This feature also works
with the existing rectangle-only detector; it does not change the comparison
algorithm or add the experimental external recognition laboratory.
