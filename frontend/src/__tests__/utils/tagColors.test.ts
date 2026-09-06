import { describe, it, expect } from 'vitest';
import { TAG_PALETTE, tagChipStyle } from '../../utils/tagColors';

describe('tag colours', () => {
  it('has ten distinct six-digit hex swatches', () => {
    expect(TAG_PALETTE).toHaveLength(10);
    expect(new Set(TAG_PALETTE.map((s) => s.hex)).size).toBe(10);
    for (const swatch of TAG_PALETTE) expect(swatch.hex).toMatch(/^#[0-9a-f]{6}$/);
  });

  it('tints the chip from the hex and stays neutral without one', () => {
    expect(tagChipStyle('#f59e0b')).toEqual({ backgroundColor: '#f59e0b26', color: '#f59e0b', borderColor: '#f59e0b59' });
    expect(tagChipStyle(null)).toBeUndefined();
    expect(tagChipStyle(undefined)).toBeUndefined();
  });
});
