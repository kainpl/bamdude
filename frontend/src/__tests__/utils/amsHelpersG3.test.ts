/**
 * Tests for the AMS helpers touched by the G3 upstream port (v0.2.4.9 → v1.2.5):
 *
 * - `resolveSlotNozzleDiameter` (upstream #1899) — the Configure-AMS-Slot picker
 *   was hardwired to 0.4mm, so a 0.6 machine could only set 0.4 profiles on its
 *   trays. It now resolves the nozzle actually feeding that AMS unit.
 * - `getAmsLabel` A2L branch — the backend normalises the AMS-Lite's physical
 *   unit id 16 to 6 at MQTT ingest, so id 6 must render as "AMS Lite".
 */
import { describe, it, expect } from 'vitest';

import { getAmsLabel, resolveSlotNozzleDiameter } from '../../utils/amsHelpers';

describe('resolveSlotNozzleDiameter', () => {
  it('returns undefined when the printer has not reported nozzle hardware', () => {
    // Caller keeps its own default rather than being told a wrong diameter.
    expect(resolveSlotNozzleDiameter(undefined, 0)).toBeUndefined();
    expect(resolveSlotNozzleDiameter(null, 0)).toBeUndefined();
    expect(resolveSlotNozzleDiameter({ nozzles: [] }, 0)).toBeUndefined();
  });

  it('returns the primary nozzle on a single-nozzle printer', () => {
    expect(resolveSlotNozzleDiameter({ nozzles: [{ nozzle_diameter: '0.6' }] }, 0)).toBe('0.6');
  });

  it('honours ams_extruder_map on a dual-nozzle printer', () => {
    const status = {
      nozzles: [{ nozzle_diameter: '0.4' }, { nozzle_diameter: '0.8' }],
      ams_extruder_map: { '0': 0, '1': 1 },
    };
    expect(resolveSlotNozzleDiameter(status, 0)).toBe('0.4');
    expect(resolveSlotNozzleDiameter(status, 1)).toBe('0.8');
  });

  it('falls back to the primary nozzle when the AMS has no map entry', () => {
    const status = {
      nozzles: [{ nozzle_diameter: '0.4' }, { nozzle_diameter: '0.8' }],
      ams_extruder_map: { '1': 1 },
    };
    expect(resolveSlotNozzleDiameter(status, 0)).toBe('0.4');
  });

  it('falls back to the primary nozzle when the mapped index is missing', () => {
    const status = {
      nozzles: [{ nozzle_diameter: '0.4' }],
      ams_extruder_map: { '0': 1 },
    };
    expect(resolveSlotNozzleDiameter(status, 0)).toBe('0.4');
  });

  it('treats an empty diameter string as unknown', () => {
    expect(resolveSlotNozzleDiameter({ nozzles: [{ nozzle_diameter: '' }] }, 0)).toBeUndefined();
  });
});

describe('getAmsLabel — A2L AMS Lite', () => {
  it('labels normalised unit 6 as "AMS Lite"', () => {
    expect(getAmsLabel(6, 4)).toBe('AMS Lite');
    expect(getAmsLabel('6', 4)).toBe('AMS Lite');
  });

  it('leaves every other unit id unchanged', () => {
    expect(getAmsLabel(0, 4)).toBe('AMS-A');
    expect(getAmsLabel(1, 4)).toBe('AMS-B');
    expect(getAmsLabel(128, 1)).toBe('HT-A');
    expect(getAmsLabel(255, 4)).toBe('External');
  });
});
