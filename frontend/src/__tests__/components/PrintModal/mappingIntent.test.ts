import { describe, expect, it } from 'vitest';
import { mappingWithManualChoices, targetHasManualMapping } from '../../../components/PrintModal/mappingIntent';
const base = { printerCount: 2, multiPlate: false, plateId: 1, manual: { 1: 0 }, byPlate: {}, storedPinned: false };
describe('manual intent belongs to one target', () => {
  it('keeps an explicitly rejected tray in the request for authoritative validation', () => {
    expect(mappingWithManualChoices([-1, -1, -1], { 3: 254 })).toEqual([-1, -1, 254]);
    expect(mappingWithManualChoices(undefined, { 3: 254 })).toEqual([-1, -1, 254]);
    expect(mappingWithManualChoices(undefined, {})).toBeUndefined();
  });
  it('does not pin another default or auto-configured printer from a global map', () => {
    expect(targetHasManualMapping(base)).toBe(false);
    expect(targetHasManualMapping({ ...base, config: { useDefault: false, autoConfigured: true, manualMappings: { 1: 2 } } })).toBe(false);
    expect(targetHasManualMapping({ ...base, config: { useDefault: false, autoConfigured: false, manualMappings: { 1: 2 } } })).toBe(true);
  });
  it('keeps a stored pin when editing without a new selection', () => {
    expect(targetHasManualMapping({ ...base, printerCount: 1, manual: {}, storedPinned: true })).toBe(true);
  });
  it('does not leak a pin between plates or emit a phantom multi-plate fan-out pin', () => {
    const plates = { ...base, printerCount: 1, multiPlate: true, byPlate: { 1: { 1: 0 } } };
    expect(targetHasManualMapping(plates)).toBe(true);
    expect(targetHasManualMapping({ ...plates, plateId: 2 })).toBe(false);
    expect(targetHasManualMapping({ ...plates, printerCount: 2 })).toBe(false);
  });
});
