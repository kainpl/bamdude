export function getPrinterImage(model: string | null | undefined): string {
  if (!model) return '/img/printers/default.png';
  const m = model.toLowerCase().replace(/\s+/g, '');
  if (m.includes('a2l') || m === 'n9') return '/img/printers/a2l.png';
  if (m.includes('x2d') || m === 'n6') return '/img/printers/x2d.png';
  if (m.includes('x1e')) return '/img/printers/x1e.png';
  if (m.includes('x1c') || m.includes('x1carbon')) return '/img/printers/x1c.png';
  if (m.includes('x1')) return '/img/printers/x1c.png';
  if (m.includes('h2dpro') || m.includes('h2d-pro')) return '/img/printers/h2dpro.png';
  if (m.includes('h2d')) return '/img/printers/h2d.png';
  if (m.includes('h2c')) return '/img/printers/h2c.png';
  if (m.includes('h2s')) return '/img/printers/h2d.png';
  if (m.includes('p2s')) return '/img/printers/p2s.png';
  if (m.includes('p1s')) return '/img/printers/p1s.png';
  if (m.includes('p1p')) return '/img/printers/p1p.png';
  if (m.includes('a1mini')) return '/img/printers/a1mini.png';
  if (m.includes('a1')) return '/img/printers/a1.png';
  return '/img/printers/default.png';
}

// Models with a confirmed door-open sensor exposed via MQTT.
// Mirrors the backend door-sensor sets (printer_models.py). The X1 family
// (X1/X1C/X1E) reports door state on home_flag bit 23; X2D and P2S on stat
// bit 23. The backend resolves door_open from the right field per model; this
// set only decides whether to show the badge. P2S is inferred from sharing
// X2D's exact door-sensor part (pending hardware confirmation). The H2 family
// has sensors but an unverified MQTT signal; P1S has no sensor; open-frame
// models (P1P/A1/A1 Mini) have no door hardware — none belong here.
//
// To add a model: verify on a real printer that the bit actually flips when
// the enclosure opens/closes, then update both this set AND the backend
// counterpart. Never add on protocol speculation.
const DOOR_SENSOR_MODELS = new Set(['X1', 'X1C', 'X1E', 'X2D', 'P2S']);

export function hasDoorSensor(model: string | null | undefined): boolean {
  if (!model) return false;
  const normalized = model.trim().toUpperCase().replace(/[\s-]/g, '');
  return DOOR_SENSOR_MODELS.has(normalized);
}

// Map SSDP model codes (e.g. "BL-P001") to display names (e.g. "X1C") that
// match what slicers stamp into the 3MF `sliced_for_model` metadata. Used
// for compatibility checks before dispatching a sliced file to a printer.
//
// A superset of the backend's `PRINTER_MODEL_ID_MAP`
// (backend/app/utils/printer_models.py) — every backend row is here.
// `BL-P003` is frontend-only (the backend map lacks it; removing it here
// buys nothing).
const MODEL_DISPLAY_MAP: Record<string, string> = {
  // H2 Series
  'O1D': 'H2D',
  'O1E': 'H2D Pro',
  'O2D': 'H2D Pro',
  'O1C': 'H2C',
  'O1C2': 'H2C',
  'O1S': 'H2S',
  // X1 Series (BS configs: X1C=BL-P001, X1=BL-P002, X1E=C13)
  'BL-P001': 'X1C',
  'BL-P002': 'X1',
  'BL-P003': 'X1E',
  'C13': 'X1E',
  // X2 Series
  'N6': 'X2D',
  // A2 Series
  'N9': 'A2L',
  // P Series (BS configs: P1P=C11, P1S=C12, P2S=N7)
  'C11': 'P1P',
  'C12': 'P1S',
  'N7': 'P2S',
  // A1 Series
  'N2S': 'A1',
  'N1': 'A1 Mini',
  'A11': 'A1',
  'A12': 'A1 Mini',
  'A04': 'A1 Mini',
  // Direct matches (already in display form)
  'X1C': 'X1C',
  'X1': 'X1',
  'X1E': 'X1E',
  'X2D': 'X2D',
  'P1S': 'P1S',
  'P1P': 'P1P',
  'P2S': 'P2S',
  'A1': 'A1',
  'A1 Mini': 'A1 Mini',
  'A2L': 'A2L',
  'H2D': 'H2D',
  'H2D Pro': 'H2D Pro',
  'H2C': 'H2C',
  'H2S': 'H2S',
};

export function mapModelCode(ssdpModel: string | null | undefined): string {
  if (!ssdpModel) return '';
  return MODEL_DISPLAY_MAP[ssdpModel] || ssdpModel;
}

// The OTHER spelling a printer model arrives in: the long marketing name a
// `Printer.model` column or a 3MF's `printer_model` carries. Mirrors the
// backend's `PRINTER_MODEL_MAP` (backend/app/utils/printer_models.py) — keep
// the two in step.
const MODEL_LONG_NAME_MAP: Record<string, string> = {
  'Bambu Lab X1 Carbon': 'X1C',
  'Bambu Lab X1': 'X1',
  'Bambu Lab X1E': 'X1E',
  'Bambu Lab P1S': 'P1S',
  'Bambu Lab P1P': 'P1P',
  'Bambu Lab P2S': 'P2S',
  'Bambu Lab A1': 'A1',
  'Bambu Lab A1 Mini': 'A1 Mini',
  'Bambu Lab A1 mini': 'A1 Mini',
  // Bambu cloud rolled out a terse model-code rename mid-2026 (#1649).
  'Bambu Lab A1M': 'A1 Mini',
  'Bambu Lab H2D': 'H2D',
  'Bambu Lab H2D Pro': 'H2D Pro',
  'Bambu Lab H2C': 'H2C',
  'Bambu Lab H2S': 'H2S',
  'Bambu Lab X2D': 'X2D',
  'Bambu Lab A2L': 'A2L',
};

/**
 * Any spelling of a printer model → the short name everything else compares.
 *
 * The frontend mirror of the backend's `normalize_model_name`, and it must
 * resolve the two maps in the SAME order: internal codes FIRST (`mapModelCode`
 * returns unknown input unchanged, so a "C12" that reached the long-name map
 * first would come back as "C12" and never be recognised), then the long
 * marketing names, then the "Bambu Lab " prefix stripped off a model neither
 * map knows.
 *
 * ⚠️ **Both sides of a comparison go through this, or neither.** A printer row
 * says "Bambu Lab X1 Carbon" and a 3MF says "X1C" for the same machine — the
 * plan block matched a row's alternative files against the chosen printer with
 * `mapModelCode` alone, which passes the long name straight through, so the
 * printer-first menu silently kept the row's own file for every printer whose
 * `model` column carries the long spelling.
 *
 * Unknown input comes back as itself rather than empty: a model we have never
 * seen is not a reason to lose the operator's answer.
 */
export function normalizeModelName(model: string | null | undefined): string {
  const byCode = mapModelCode(model);
  if (!byCode) return '';
  const byName = MODEL_LONG_NAME_MAP[byCode];
  if (byName) return byName;
  return byCode.replace(/^Bambu Lab\s+/, '').trim() || byCode;
}

export function getWifiStrength(rssi: number): { labelKey: string; color: string; bars: number } {
  if (rssi >= -50) return { labelKey: 'printers.wifiSignal.excellent', color: 'text-bambu-green', bars: 4 };
  if (rssi >= -60) return { labelKey: 'printers.wifiSignal.good', color: 'text-bambu-green', bars: 3 };
  if (rssi >= -70) return { labelKey: 'printers.wifiSignal.fair', color: 'text-yellow-600 dark:text-yellow-400', bars: 2 };
  if (rssi >= -80) return { labelKey: 'printers.wifiSignal.weak', color: 'text-orange-600 dark:text-orange-400', bars: 1 };
  return { labelKey: 'printers.wifiSignal.veryWeak', color: 'text-red-600 dark:text-red-400', bars: 1 };
}

// Ceiling for the chamber targets the UI asks for without a printer in hand:
// the preheat filament map (global, one target per filament type) and the
// per-print override (entered before a printer is chosen). Mirrors backend
// MAX_CHAMBER_TEMP_C in backend/app/utils/temperature_limits.py — keep the two
// in sync.
//
// ⚠️ NOT for the manual chamber control on the printer card. That one bounds
// itself from the printer's own reported limits, which is the better answer;
// a flat number there would be a step backwards.
export const MAX_CHAMBER_TEMP_C = 65;
