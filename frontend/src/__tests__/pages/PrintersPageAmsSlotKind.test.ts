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

  // upstream #2527 — tray_exist_bits is firmware's authoritative presence signal
  // and overrides the state heuristic when available.
  describe('tray_exist_bits (exists)', () => {
    it('returns "reset" for a present-but-unidentified spool despite state 9', () => {
      // The non-RFID case: the AMS reports an empty tray_type + state=9, which is
      // structurally identical to a truly empty slot, but the bitmask says a
      // spool is physically there — Studio draws "?" here, so must we.
      expect(getEmptySlotKind({ tray_type: '', state: 9, exists: true })).toBe('reset');
    });

    it('returns "physical" when the bitmask says the slot is empty', () => {
      expect(getEmptySlotKind({ tray_type: null, state: 11, exists: false })).toBe('physical');
    });

    it('still returns null for a configured slot', () => {
      expect(getEmptySlotKind({ tray_type: 'PLA', state: 9, exists: true })).toBeNull();
    });

    it('falls back to the state heuristic when the bitmask was unavailable', () => {
      expect(getEmptySlotKind({ tray_type: null, state: 9, exists: null })).toBe('physical');
      expect(getEmptySlotKind({ tray_type: null, state: 11, exists: null })).toBe('reset');
    });
  });
});
