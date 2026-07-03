/**
 * Tests for getEmptySlotKind — the empty-AMS-slot classification (#1694).
 *
 * A slot with no `tray_type` used to render identically to a truly empty slot
 * ("-"), so a spool that was physically loaded but never had its filament type
 * configured looked empty. `getEmptySlotKind` (in utils/amsHelpers) distinguishes
 * the two using the firmware tray state (9 = empty, 10 = present-but-not-fed,
 * 11 = loaded); PrintersPage's compact label, slot circle and empty-slot hover
 * card all branch on the result so a loaded-but-unconfigured slot shows "?" with
 * an amber accent instead of looking empty.
 */
import { describe, it, expect } from 'vitest';

import { getEmptySlotKind } from '../../utils/amsHelpers';

describe('getEmptySlotKind', () => {
  it('returns null for a configured slot regardless of state', () => {
    expect(getEmptySlotKind({ tray_type: 'PLA', state: 11 })).toBeNull();
    expect(getEmptySlotKind({ tray_type: 'PETG', state: 9 })).toBeNull();
  });

  it('returns "physical" for a firmware-empty slot (state 9)', () => {
    expect(getEmptySlotKind({ tray_type: null, state: 9 })).toBe('physical');
  });

  it('returns "physical" for a present-but-not-fed slot (state 10)', () => {
    expect(getEmptySlotKind({ tray_type: null, state: 10 })).toBe('physical');
  });

  it('returns "reset" for a loaded-but-unconfigured slot (state 11, no type)', () => {
    expect(getEmptySlotKind({ tray_type: null, state: 11 })).toBe('reset');
  });

  it('returns "reset" when state is missing but no type is set', () => {
    // Without a firmware state we can't prove the slot is physically empty, so
    // we err toward "?" (loaded-but-unconfigured) rather than silently "-".
    expect(getEmptySlotKind({ tray_type: null, state: null })).toBe('reset');
    expect(getEmptySlotKind({ tray_type: null })).toBe('reset');
    expect(getEmptySlotKind(undefined)).toBe('reset');
  });

  it('treats an empty-string tray_type as unconfigured', () => {
    expect(getEmptySlotKind({ tray_type: '', state: 11 })).toBe('reset');
  });
});
