/**
 * Temp Tower start/end defaults by filament type.
 *
 * Verbatim from the Bambu Studio Temp_Calibration_Dlg presets
 * (`calib_dlg.cpp` `on_filament_type_changed`). BS reads the type off a
 * radio box; BamDude derives it from the selected filament preset / the
 * loaded AMS slot's material. The temperature *descends* up the tower, so
 * `start > end`.
 */
export interface TempRange {
  start: number;
  end: number;
}

/**
 * Map a filament type / name string to its BS Temp Tower default range.
 *
 * `s` is matched most-specific-first — PET-CF / PA-CF before PETG / PA,
 * PCTG before PETG — so a composite "type + preset name" string resolves
 * correctly. Unknown / custom filaments fall back to the PLA range, which
 * is what the BS dialog does for its "Custom" radio option.
 */
export function tempDefaultsForFilament(s: string): TempRange {
  const n = s.toUpperCase();
  if (n.includes('PET-CF') || n.includes('PETCF')) return { start: 320, end: 280 };
  if (n.includes('PA-CF') || n.includes('PACF') || n.includes('PAHT') || n.includes('NYLON'))
    return { start: 320, end: 280 };
  if (n.includes('PCTG')) return { start: 280, end: 240 };
  if (n.includes('PETG')) return { start: 250, end: 230 };
  if (n.includes('TPU')) return { start: 240, end: 210 };
  if (n.includes('ABS') || n.includes('ASA')) return { start: 270, end: 230 };
  if (n.includes('PLA')) return { start: 230, end: 190 };
  return { start: 230, end: 190 };
}
