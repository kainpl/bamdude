import { describe, it, expect, beforeEach } from 'vitest';
import {
  getColorName,
  hexToColorName,
  resolveSpoolColorName,
  setColorCatalog,
  __resetColorCatalogForTests,
  parseFilamentColor,
  isLightColor,
} from '../../utils/colors';

beforeEach(() => {
  __resetColorCatalogForTests();
});

describe('hexToColorName (HSL fallback)', () => {
  it('returns Unknown for empty/null', () => {
    expect(hexToColorName(null)).toBe('Unknown');
    expect(hexToColorName('')).toBe('Unknown');
    expect(hexToColorName('abc')).toBe('Unknown');
  });

  it('classifies obvious colors via HSL', () => {
    expect(hexToColorName('000000')).toBe('Black');
    expect(hexToColorName('ffffff')).toBe('White');
    expect(hexToColorName('ff0000')).toBe('Red');
    expect(hexToColorName('00ff00')).toBe('Green');
    expect(hexToColorName('0000ff')).toBe('Blue');
    expect(hexToColorName('ffff00')).toBe('Yellow');
  });

  it('handles leading hash', () => {
    expect(hexToColorName('#000000')).toBe('Black');
  });
});

describe('runtime color catalog', () => {
  it('getColorName falls back to HSL when catalog empty', () => {
    expect(getColorName('ff0000')).toBe('Red');
  });

  it('getColorName uses catalog when populated', () => {
    setColorCatalog({ 'ff0000': 'Cherry Red Custom' });
    expect(getColorName('ff0000')).toBe('Cherry Red Custom');
  });

  it('setColorCatalog normalizes keys (strips #, lowercases, truncates to 6)', () => {
    setColorCatalog({
      '#FF0000': 'A',
      'AABBCCDD': 'B',
      'short': 'C',
    });
    expect(getColorName('ff0000')).toBe('A');
    expect(getColorName('AABBCC')).toBe('B');
    // 'short' is too short → never indexed → falls through to HSL
    expect(getColorName('short')).toBe('Unknown');
  });

  it('catalog name takes priority over HSL', () => {
    setColorCatalog({ '00ff00': 'Bambu Mistletoe' });
    expect(getColorName('00ff00')).toBe('Bambu Mistletoe');
  });

  it('replacing the catalog clears previous entries', () => {
    setColorCatalog({ 'ff0000': 'First' });
    setColorCatalog({ '00ff00': 'Second' });
    expect(getColorName('ff0000')).toBe('Red'); // back to HSL fallback
    expect(getColorName('00ff00')).toBe('Second');
  });
});

describe('resolveSpoolColorName', () => {
  it('uses readable color_name directly', () => {
    expect(resolveSpoolColorName('Cherry Pink', 'ffffffff')).toBe('Cherry Pink');
  });

  it('ignores Bambu code-style color_name in favor of catalog', () => {
    setColorCatalog({ 'aabbcc': 'Catalog Lookup' });
    expect(resolveSpoolColorName('A06-D0', 'aabbccff')).toBe('Catalog Lookup');
  });

  it('returns null when code-style color_name and no rgba match', () => {
    expect(resolveSpoolColorName('A06-D0', null)).toBeNull();
    expect(resolveSpoolColorName('A06-D0', 'ddddddff')).toBeNull();
  });

  it('returns null when no color_name and no rgba match in catalog', () => {
    expect(resolveSpoolColorName(null, null)).toBeNull();
    expect(resolveSpoolColorName(null, 'ddddddff')).toBeNull();
  });

  it('looks up rgba via catalog when color_name is null', () => {
    setColorCatalog({ 'ff0000': 'Fire Engine Red' });
    expect(resolveSpoolColorName(null, 'ff0000ff')).toBe('Fire Engine Red');
  });
});

describe('parseFilamentColor', () => {
  it('returns null for empty/transparent', () => {
    expect(parseFilamentColor('')).toBeNull();
    expect(parseFilamentColor('00000000')).toBeNull();
    expect(parseFilamentColor('ff000000')).toBeNull(); // alpha = 0
  });

  it('parses RRGGBBAA', () => {
    expect(parseFilamentColor('ff0000ff')).toBe('rgba(255, 0, 0, 1)');
    expect(parseFilamentColor('00ff007f')).toContain('rgba(0, 255, 0,');
  });

  it('parses RRGGBB without alpha', () => {
    expect(parseFilamentColor('00ff00')).toBe('rgba(0, 255, 0, 1)');
  });
});

describe('isLightColor', () => {
  it('returns false for null/short', () => {
    expect(isLightColor(null)).toBe(false);
    expect(isLightColor('abc')).toBe(false);
  });

  it('classifies bright colors as light', () => {
    expect(isLightColor('ffffff')).toBe(true);
    expect(isLightColor('ffff00')).toBe(true);
  });

  it('classifies dark colors as not light', () => {
    expect(isLightColor('000000')).toBe(false);
    expect(isLightColor('800000')).toBe(false);
  });
});
