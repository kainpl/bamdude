/**
 * An AMS-HT is a unit id 128-135, never "a unit that listed one tray"
 * (upstream cacaaf4da legs 4 and 6).
 */
import { describe, expect, it } from 'vitest';
import { amsHasLoadedTray, getAmsLabel, isAmsHtUnit, trayForSlot } from '../../utils/amsHelpers';

describe('isAmsHtUnit', () => {
  it('is the unit id range 128-135', () => {
    expect([127, 128, 131, 135, 136].map(id => isAmsHtUnit(id))).toEqual([false, true, true, true, false]);
  });

  it('reads an id sent as text', () => {
    expect(isAmsHtUnit('129')).toBe(true);
    expect(isAmsHtUnit('1')).toBe(false);
  });
});

describe('getAmsLabel', () => {
  it('names a regular AMS that listed one tray as an AMS', () => {
    expect(getAmsLabel(0, 1)).toBe('AMS-A');
    expect(getAmsLabel(2, 1)).toBe('AMS-C');
  });

  it('names an AMS-HT by its unit, whatever it listed', () => {
    expect(getAmsLabel(128, 1)).toBe('HT-A');
    expect(getAmsLabel(129, 0)).toBe('HT-B');
    expect(getAmsLabel(130, 4)).toBe('HT-C');
  });

  it('keeps the AMS Lite and the external holder', () => {
    expect(getAmsLabel(6, 4)).toBe('AMS Lite');
    expect(getAmsLabel(255, 1)).toBe('External');
  });
});

describe('trayForSlot', () => {
  it('finds a tray by its id when the unit lists only some', () => {
    const trays = [{ id: 2, name: 'third' }];

    expect(trayForSlot(trays, 2)?.name).toBe('third');
    expect(trayForSlot(trays, 0)).toBeUndefined();
  });

  it('falls back to the position when the ids do not tell the trays apart', () => {
    const trays = [{ id: 0, name: 'a' }, { id: 0, name: 'b' }];

    expect(trayForSlot(trays, 1)?.name).toBe('b');
  });
});

describe('amsHasLoadedTray', () => {
  const ams = { id: 1, tray: [{ id: 0, state: 10 }, { id: 1, state: 10 }] };

  it('is false for a unit nothing is fed from', () => {
    expect(amsHasLoadedTray(ams, 255, {})).toBe(false);
    expect(amsHasLoadedTray(undefined, 5, {})).toBe(false);
  });

  it('counts tray state 11', () => {
    expect(amsHasLoadedTray({ id: 1, tray: [{ id: 0, state: 11 }] }, 255, {})).toBe(true);
  });

  it('counts the slot the printer reports as loaded', () => {
    expect(amsHasLoadedTray(ams, 5, {})).toBe(true);
    expect(amsHasLoadedTray(ams, 2, {})).toBe(false);
  });

  it('counts the slot a hotend is fed from on a two-nozzle printer', () => {
    const slots = { '0': { ams_id: 0, has_filament: true }, '1': { ams_id: 1, has_filament: true } };

    expect(amsHasLoadedTray(ams, 255, slots)).toBe(true);
    expect(amsHasLoadedTray(ams, 255, { '1': { ams_id: 1, has_filament: false } })).toBe(false);
  });

  it('knows an AMS-HT tray by its unit id', () => {
    expect(amsHasLoadedTray({ id: 129, tray: [{ id: 0, state: 10 }] }, 129, {})).toBe(true);
  });
});
