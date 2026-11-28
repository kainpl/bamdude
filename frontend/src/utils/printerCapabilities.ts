// Per-model auto-calibration capability matrix — the frontend mirror of the
// backend's sets in `backend/app/utils/printer_models.py`. Gates the print
// dialog's 3-position off/auto/on calibration control: a model NOT listed here
// shows the plain 2-position off/on toggle.
//
// Source of truth is BambuStudio's resources/printers/*.json
// (`support_bed_leveling==2`, `support_auto_flow_calibration`,
// `support_nozzle_offset_calibration`). Keep in lockstep with the backend
// frozensets — bed+flow auto and nozzle-offset auto are independent axes.

/**
 * Normalize a model name/code the same way the backend does: trim, uppercase,
 * strip spaces and dashes. e.g. "H2D Pro" → "H2DPRO", "A1-Mini" → "A1MINI".
 */
function normalizeModel(model: string | null | undefined): string {
  return (model ?? '').trim().toUpperCase().replace(/[\s-]/g, '');
}

// Bed + flow auto — same model set (BambuStudio advertises both together).
// This axis is INDEPENDENT of nozzle count: A2L / P2S / H2S are single-nozzle
// but still support auto bed-leveling / flow calibration.
const AUTO_BED_FLOW_MODELS: ReadonlySet<string> = new Set([
  // Display names (normalized)
  'A2L', 'P2S', 'H2S', 'H2C', 'H2D', 'H2DPRO', 'X2D', 'H3C', 'P3C',
  // Internal codes
  'N9', 'N7', 'O1S', 'O1C', 'O1C2', 'O1D', 'O1E', 'O2D', 'N6', 'N10', 'N11',
]);

// Nozzle-offset auto — the models with more than one hotend: the dual-nozzle
// heads and the direct-connected racks (H3C, P3C).
const AUTO_NOZZLE_OFFSET_MODELS: ReadonlySet<string> = new Set([
  // Display names (normalized)
  'H2C', 'H2D', 'H2DPRO', 'X2D', 'H3C', 'P3C',
  // Internal codes
  'O1C', 'O1C2', 'O1D', 'O1E', 'O2D', 'N6', 'N10', 'N11',
]);

export function supportsAutoBedLeveling(model: string | null | undefined): boolean {
  return AUTO_BED_FLOW_MODELS.has(normalizeModel(model));
}

export function supportsAutoFlowCali(model: string | null | undefined): boolean {
  return AUTO_BED_FLOW_MODELS.has(normalizeModel(model));
}

export function supportsAutoNozzleOffset(model: string | null | undefined): boolean {
  return AUTO_NOZZLE_OFFSET_MODELS.has(normalizeModel(model));
}

/** Which calibration steps expose the auto mode for a given model. Field names
 *  match `PrintOptions` (bed_levelling / flow_cali / nozzle_offset_cali) so the
 *  print dialog can gate each control directly. */
export interface AutoCalibrationCaps {
  bed_levelling: boolean;
  flow_cali: boolean;
  nozzle_offset_cali: boolean;
}

export function autoCalibrationCaps(model: string | null | undefined): AutoCalibrationCaps {
  return {
    bed_levelling: supportsAutoBedLeveling(model),
    flow_cali: supportsAutoFlowCali(model),
    nozzle_offset_cali: supportsAutoNozzleOffset(model),
  };
}

// Dual-nozzle models (#1682) — gate for the nozzle-offset calibration control.
// The MQTT layer forces "skip" on single-nozzle machines regardless; hiding
// the control avoids a misleading no-op. One source for the PrintModal's
// auto-mode gate AND the saved-profiles editor, so the two sets cannot drift.
const DUAL_NOZZLE_MODELS = new Set(['H2D', 'H2DPRO', 'H2C', 'X2D']);

export function isDualNozzleModel(model: string | null | undefined): boolean {
  if (!model) return false;
  return DUAL_NOZZLE_MODELS.has(model.toUpperCase().replace(/[\s-]/g, ''));
}

// A direct-connected hotend rack (H3C, P3C): one toolhead, a hotend per PrintTool.
const DIRECT_RACK_MODELS = new Set(['H3C', 'P3C', 'N10', 'N11', 'BAMBULABH3C', 'BAMBULABP3C']);

/** Whether the model has a nozzle offset to calibrate: a dual-nozzle head or a direct rack (Bambu Studio). */
export function hasNozzleOffset(model: string | null | undefined): boolean {
  return isDualNozzleModel(model) || DIRECT_RACK_MODELS.has(normalizeModel(model));
}
