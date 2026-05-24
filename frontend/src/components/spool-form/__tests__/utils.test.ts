import { describe, it, expect } from 'vitest';
import { resolveTargetFilamentId } from '../utils';

describe('resolveTargetFilamentId', () => {
  it('returns GF* codes as-is', () => {
    expect(resolveTargetFilamentId('GFG99', null)).toBe('GFG99');
  });

  it('strips the S from GFS* setting ids', () => {
    expect(resolveTargetFilamentId('GFSG99', null)).toBe('GFG99');
  });

  it('prefers a custom preset base_id over its own filament_id', () => {
    expect(
      resolveTargetFilamentId('P285e239', { base_id: 'GFSG99', filament_id: 'P285e239' }),
    ).toBe('GFG99');
  });

  it('falls back to the custom filament_id when no base_id', () => {
    expect(
      resolveTargetFilamentId('P285e239', { base_id: null, filament_id: 'GFX01' }),
    ).toBe('GFX01');
  });

  it('returns null when nothing resolvable', () => {
    expect(resolveTargetFilamentId(null, null)).toBe(null);
    expect(resolveTargetFilamentId('PETG', null)).toBe(null);
  });
});
