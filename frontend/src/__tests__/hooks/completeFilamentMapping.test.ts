import { describe, expect, it } from 'vitest';
import { buildAmsMapping, buildFilamentComparison, type LoadedFilament } from '../../hooks/useFilamentMapping';
const tray = (id: number, profile: string, color = '#FF0000'): LoadedFilament => ({
  globalTrayId: id, trayId: id, amsId: 0, type: 'PLA', color, colorName: '', label: String(id),
  trayInfoIdx: profile, isHt: false, isExternal: false,
});
describe('complete assignment under routing policy', () => {
  it('does not let the flexible OFF channel steal the constrained channel source', () => {
    const requirements = { filaments: [
      { slot_id: 1, type: 'PLA', color: '#FF0000', used_grams: 1, strict_profile_match: true },
      { slot_id: 2, type: 'PLA', color: '#FF0000', used_grams: 1, strict_profile_match: true, tray_info_idx: 'A' },
    ] };
    expect(buildAmsMapping(buildFilamentComparison(requirements, [tray(0, 'A'), tray(1, 'B')], {}))).toEqual([1, 0]);
  });
  it('ranks exact colour ahead of same profile even under OFF', () => {
    const requirements = { filaments: [{ slot_id: 1, type: 'PLA', color: '#FF0000', used_grams: 1,
      strict_profile_match: true, tray_info_idx: 'A' }] };
    expect(buildAmsMapping(buildFilamentComparison(requirements, [tray(0, 'A', '#E61414'), tray(1, '')], {}))).toEqual([1]);
  });
  it('also finds the full assignment for mixed colour constraints under ON', () => {
    const requirements = { filaments: [
      { slot_id: 1, type: 'PLA', color: '#FF0000', used_grams: 1, ignore_profile: true },
      { slot_id: 2, type: 'PLA', color: '#00FF00', used_grams: 1, ignore_profile: true, strict_color_match: true },
    ] };
    expect(buildAmsMapping(buildFilamentComparison(requirements, [tray(0, 'A', '#00FF00'), tray(1, 'B', '#0000FF')], {}))).toEqual([1, 0]);
  });
});
