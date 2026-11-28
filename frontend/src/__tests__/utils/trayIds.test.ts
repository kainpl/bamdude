/**
 * `utils/trayIds.ts` answers the same table as its twin `backend/app/utils/tray_ids.py`
 * (`backend/tests/unit/test_tray_ids.py` reads the same file).
 */
import { describe, it, expect } from 'vitest';
import cases from '../fixtures/trayIdCases.json';
import {
  acceptsSlot,
  externalLabel,
  externalPrintTool,
  externalSlotIndex,
  externalTrayId,
  globalTrayId,
  slotKey,
  unitName,
  type TrayLayout,
} from '../../utils/trayIds';
import { getGlobalTrayId, formatSlotLabel, amsSideBadge } from '../../utils/amsHelpers';

type LayoutName = keyof typeof cases.layouts;

function layout(name: string): TrayLayout {
  const spec = cases.layouts[name as LayoutName];
  return {
    units: spec.units.map(([uid, kind]) => [uid, kind] as [number, number]),
    directRack: spec.direct_rack,
    rackSize: spec.rack_size,
  };
}

describe('trayIds — the shared table', () => {
  it.each(cases.global)('global id of ($ams, $slot, type $type) is $global', ({ ams, slot, type, global }) => {
    expect(globalTrayId(ams, slot, type)).toBe(global);
  });

  it.each(cases.external)('external input $id', ({ id, index, print_tool, label }) => {
    expect(externalSlotIndex(id)).toBe(index);
    expect(externalTrayId(index)).toBe(id);
    expect(externalPrintTool(id)).toBe(print_tool);
    expect(externalLabel(id)).toBe(label);
  });

  it.each(cases.slot_key)('slot key of $global on $layout', ({ layout: name, global, key }) => {
    expect(slotKey(layout(name), global)).toEqual(key);
  });

  it.each(cases.accepts)('$layout accepts ($ams, $slot): $ok', ({ layout: name, ams, slot, ok }) => {
    expect(acceptsSlot(layout(name), ams, slot)).toBe(ok);
  });

  it.each(cases.unit_name)('unit $ams is $name', ({ ams, type, name }) => {
    expect(unitName(ams, type)).toBe(name);
  });
});

describe('amsHelpers delegate to trayIds', () => {
  it('keeps every other printer as it was', () => {
    expect(getGlobalTrayId(1, 2, false)).toBe(6);
    expect(getGlobalTrayId(129, 0, false)).toBe(129);
    expect(getGlobalTrayId(255, 1, true)).toBe(255);
    expect(formatSlotLabel(0, 2, false, false)).toBe('A3');
    expect(formatSlotLabel(128, 0, true, false)).toBe('HT-A');
    expect(formatSlotLabel(255, 0, false, true)).toBe('Ext');
  });

  it('numbers and labels a rack AMS and a direct rack input', () => {
    expect(getGlobalTrayId(32, 6, false)).toBe(34);
    expect(getGlobalTrayId(255, 2, true)).toBe(247);
    expect(formatSlotLabel(32, 5, false, false)).toBe('3H-6');
    expect(formatSlotLabel(255, 8, false, true)).toBe('Ext-3');
  });

  it('puts no L/R badge on a direct rack', () => {
    expect(amsSideBadge(0, { '0': 1 }, {}, false)).toEqual({ kind: 'nozzle', side: 'L' });
    expect(amsSideBadge(0, { '0': 1 }, {}, false, true)).toBeNull();
  });
});
