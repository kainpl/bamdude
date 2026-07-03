/**
 * Tests for the empty-AMS-slot classification logic (#1694).
 *
 * A slot with no `tray_type` used to render identically to a truly empty slot
 * ("-"), so a spool that was physically loaded but never had its filament type
 * configured looked empty. `getEmptySlotKind` distinguishes the two using the
 * firmware tray state (9 = empty, 10 = present-but-not-fed, 11 = loaded):
 *   - configured slot (tray_type set)      → null
 *   - state 9 or 10 (empty / not fed)      → 'physical'  (render "-"/Empty)
 *   - otherwise (loaded, no type)          → 'reset'     (render "?")
 *
 * Mirrors the module-level getEmptySlotKind in PrintersPage.tsx; extracted here
 * for testability, matching the sibling PrintersPageFillLevel test pattern.
 */
import { describe, it, expect } from 'vitest';

function getEmptySlotKind(
  tray: { tray_type?: string | null; state?: number | null } | undefined,
): 'physical' | 'reset' | null {
  if (tray?.tray_type) return null;
  const state = tray?.state ?? null;
  return state === 9 || state === 10 ? 'physical' : 'reset';
}

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
