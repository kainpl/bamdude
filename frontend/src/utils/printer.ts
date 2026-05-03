export function getPrinterImage(model: string | null | undefined): string {
  if (!model) return '/img/printers/default.png';
  const m = model.toLowerCase().replace(/\s+/g, '');
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
// Mirrors backend/app/utils/printer_models.py::DOOR_SENSOR_MODELS — only X1
// family has a reverse-engineered signal on home_flag bit 23. Bit 23 of
// `stat` on other enclosed models (P1S/P2S/H2*) is undocumented and
// unreliable, so we don't show a door badge there (it would either flap or
// stay stuck on "Closed", misleading the operator).
//
// Open-frame models (P1P, A1, A1 Mini) MUST NOT appear here — they have no
// door hardware at all.
//
// To add a model: verify on a real printer that the bit actually flips when
// the enclosure opens/closes, then update both this set AND the backend
// counterpart. Never add on protocol speculation.
const DOOR_SENSOR_MODELS = new Set(['X1', 'X1C', 'X1E']);

export function hasDoorSensor(model: string | null | undefined): boolean {
  if (!model) return false;
  const normalized = model.trim().toUpperCase().replace(/[\s-]/g, '');
  return DOOR_SENSOR_MODELS.has(normalized);
}

// Map SSDP model codes (e.g. "BL-P001") to display names (e.g. "X1C") that
// match what slicers stamp into the 3MF `sliced_for_model` metadata. Used
// for compatibility checks before dispatching a sliced file to a printer.
const MODEL_DISPLAY_MAP: Record<string, string> = {
  // H2 Series
  'O1D': 'H2D',
  'O1E': 'H2D Pro',
  'O2D': 'H2D Pro',
  'O1C': 'H2C',
  'O1C2': 'H2C',
  'O1S': 'H2S',
  // X1 Series
  'BL-P001': 'X1C',
  'BL-P002': 'X1',
  'BL-P003': 'X1E',
  // X2 Series
  'N6': 'X2D',
  // P Series
  'C11': 'P1S',
  'C12': 'P1P',
  'C13': 'P2S',
  // A1 Series
  'N2S': 'A1',
  'N1': 'A1 Mini',
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
  'H2D': 'H2D',
  'H2D Pro': 'H2D Pro',
  'H2C': 'H2C',
  'H2S': 'H2S',
};

export function mapModelCode(ssdpModel: string | null | undefined): string {
  if (!ssdpModel) return '';
  return MODEL_DISPLAY_MAP[ssdpModel] || ssdpModel;
}

export function getWifiStrength(rssi: number): { labelKey: string; color: string; bars: number } {
  if (rssi >= -50) return { labelKey: 'printers.wifiSignal.excellent', color: 'text-bambu-green', bars: 4 };
  if (rssi >= -60) return { labelKey: 'printers.wifiSignal.good', color: 'text-bambu-green', bars: 3 };
  if (rssi >= -70) return { labelKey: 'printers.wifiSignal.fair', color: 'text-yellow-400', bars: 2 };
  if (rssi >= -80) return { labelKey: 'printers.wifiSignal.weak', color: 'text-orange-400', bars: 1 };
  return { labelKey: 'printers.wifiSignal.veryWeak', color: 'text-red-400', bars: 1 };
}
