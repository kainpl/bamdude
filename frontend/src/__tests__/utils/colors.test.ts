import { describe, it, expect, beforeEach } from 'vitest';
import {
  disambiguateColorNames,
  getColorName,
  hexToColorName,
  resolveSpoolColorName,
  setColorCatalog,
  __resetColorCatalogForTests,
  parseFilamentColor,
  isLightColor,
  getSwatchStyle,
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

  // #1545: transparent filament is reported as `00000000` (alpha=00). Without
  // the alpha-aware short-circuit it would fall through to HSL bucketing and
  // resolve to "Black" because the RGB happens to be 000000.
  it('classifies any alpha=00 rgba as Clear', () => {
    expect(hexToColorName('00000000')).toBe('Clear');
    expect(hexToColorName('FF000000')).toBe('Clear');
    expect(hexToColorName('#abcdef00')).toBe('Clear');
  });

  it('still classifies fully opaque colors via HSL even when alpha is FF', () => {
    expect(hexToColorName('000000FF')).toBe('Black');
    expect(hexToColorName('FFFFFFFF')).toBe('White');
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

  // #1545: alpha=00 must short-circuit catalog lookup too — otherwise a catalog
  // entry on the underlying RGB would mislabel transparent filament.
  it('getColorName returns Clear for transparent rgba regardless of catalog entry', () => {
    setColorCatalog({ '000000': 'Inky Night' });
    expect(getColorName('00000000')).toBe('Clear');
    expect(getColorName('000000FF')).toBe('Inky Night');
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

  // #1545: alpha=00 short-circuits to Clear even when color_name is a code.
  it('returns Clear for transparent rgba even when color_name is a code', () => {
    expect(resolveSpoolColorName('A99-Z9', '00000000')).toBe('Clear');
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

  // #1545: transparent swatches paint over a light checkerboard, so treat them
  // as light for text-contrast purposes.
  it('treats alpha=00 as light', () => {
    expect(isLightColor('00000000')).toBe(true);
  });
});

describe('getSwatchStyle (#1545, upstream 73912d4f #2912)', () => {
  const CHECKERBOARD = 'repeating-conic-gradient(#979797 0% 25%, #f5f5f5 0% 50%)';

  it('falls back to neutral grey for missing or unparseable input', () => {
    expect(getSwatchStyle(null)).toEqual({ backgroundColor: '#808080' });
    expect(getSwatchStyle(undefined)).toEqual({ backgroundColor: '#808080' });
    expect(getSwatchStyle('')).toEqual({ backgroundColor: '#808080' });
    expect(getSwatchStyle('ABC')).toEqual({ backgroundColor: '#808080' });
  });

  it('paints an opaque colour flat, with or without the FF byte', () => {
    expect(getSwatchStyle('FF0000')).toEqual({ backgroundColor: '#FF0000' });
    expect(getSwatchStyle('FF0000FF')).toEqual({ backgroundColor: '#FF0000' });
    expect(getSwatchStyle('#FF0000FF')).toEqual({ backgroundColor: '#FF0000' });
  });

  it('shows the checkerboard alone for a fully transparent colour', () => {
    expect(getSwatchStyle('00000000')).toEqual({ backgroundImage: CHECKERBOARD, backgroundSize: '8px 8px' });
  });

  it('layers a partly translucent colour over the checkerboard', () => {
    // It used to fall through to the RGB prefix, so a 50%-alpha spool rendered
    // identically to an opaque one — reachable now that Spoolman stores alpha.
    const style = getSwatchStyle('FF000080');
    expect(style.backgroundColor).toBeUndefined();
    expect(style.backgroundImage).toBe(`linear-gradient(#FF000080, #FF000080), ${CHECKERBOARD}`);
    expect(style.backgroundSize).toBe('100% 100%, 8px 8px');
  });

  it('treats the alpha byte case-insensitively', () => {
    expect(getSwatchStyle('ff0000ff')).toEqual({ backgroundColor: '#ff0000' });
    expect(getSwatchStyle('ff000000')).toEqual({ backgroundImage: CHECKERBOARD, backgroundSize: '8px 8px' });
  });
});

// Upstream e4a9ef45 (#3090) — Spoolman has no colour-name field, so every
// Spoolman-backed spool arrives with its subtype sitting in color_name. It reads
// like a name and is not one.
describe('resolveSpoolColorName — a name the backend synthesised from the subtype', () => {
  beforeEach(() => {
    setColorCatalog({ '5f6367': 'Titan Gray' });
  });

  it('loses to the catalog', () => {
    expect(resolveSpoolColorName('Silk+', '5F6367FF', true)).toBe('Titan Gray');
  });

  it('still wins over nothing when the hex is unknown', () => {
    // It at least says what is on the spool; the catalog covers only what
    // someone put in it.
    expect(resolveSpoolColorName('Silk+', '123456FF', true)).toBe('Silk+');
  });

  it('is not consulted when the flag is absent', () => {
    // Every existing caller keeps the old behaviour: a stored name is the
    // user's and is used as given.
    expect(resolveSpoolColorName('Silk+', '5F6367FF')).toBe('Silk+');
  });

  it('does not resurrect a Bambu internal code', () => {
    expect(resolveSpoolColorName('A99-Z9', '123456FF', true)).toBeNull();
  });
});

describe('disambiguateColorNames', () => {
  // upstream #2941: a slicer profile asked for a near-pure blue while the AMS slot held
  // Bambu's navy Blue. Both resolve to the name "Blue", so the mismatch warning
  // sat between two identical labels and read as a contradiction.
  const SLICER_BLUE = '#0028FF';
  const BAMBU_BLUE = '#0A2989';

  it('qualifies both sides with hex when the names collide', () => {
    expect(
      disambiguateColorNames({ name: 'Blue', hex: SLICER_BLUE }, { name: 'Blue', hex: BAMBU_BLUE }),
    ).toEqual(['Blue (#0028FF)', 'Blue (#0A2989)']);
  });

  it('treats names as colliding regardless of case', () => {
    expect(disambiguateColorNames({ name: 'blue', hex: SLICER_BLUE }, { name: 'Blue', hex: BAMBU_BLUE })).toEqual([
      'blue (#0028FF)',
      'Blue (#0A2989)',
    ]);
  });

  it('leaves distinct names alone', () => {
    // Once the words separate them the hex is noise, not information.
    expect(disambiguateColorNames({ name: 'Blue', hex: SLICER_BLUE }, { name: 'Navy', hex: BAMBU_BLUE })).toEqual([
      'Blue',
      'Navy',
    ]);
  });

  it('falls back to the hex for a side with no name', () => {
    expect(disambiguateColorNames({ hex: SLICER_BLUE }, { name: 'Blue', hex: BAMBU_BLUE })).toEqual([
      '#0028FF',
      'Blue',
    ]);
  });

  it('keeps the bare names when neither side has a usable hex', () => {
    // Better a repeated name than "Blue ()" twice.
    expect(disambiguateColorNames({ name: 'Blue', hex: 'nonsense' }, { name: 'Blue' })).toEqual(['Blue', 'Blue']);
  });

  it('qualifies only the side that has a hex', () => {
    expect(disambiguateColorNames({ name: 'Blue', hex: SLICER_BLUE }, { name: 'Blue' })).toEqual([
      'Blue (#0028FF)',
      'Blue',
    ]);
  });

  it('normalizes hex form, so an 8-char rgba and a bare hex read alike', () => {
    expect(disambiguateColorNames({ name: 'Blue', hex: '0028ffff' }, { name: 'Blue', hex: '#0a2989' })).toEqual([
      'Blue (#0028FF)',
      'Blue (#0A2989)',
    ]);
  });

  it('returns empty labels when there is nothing to name', () => {
    expect(disambiguateColorNames({}, {})).toEqual(['', '']);
  });
});

describe('a colour a step off the catalogue is named after its Bambu colour', () => {
  // A colour picked on the printer's screen is reported a step or two off the
  // catalogue's hex: 5A657B for Blue Grey 5B6579. No exact lookup finds it.
  const bambu = [
    { hex: '5B6579', name: 'Blue Grey', material: 'PLA Basic' },
    { hex: '0A2989', name: 'Blue', material: 'PLA Basic' },
    { hex: '8E9089', name: 'Gray', material: 'PLA Basic' },
    { hex: '8E9088', name: 'Translucent Gray', material: 'PETG Translucent' },
    { hex: 'F5547C', name: 'Hot Pink', material: 'PLA Matte' },
  ];
  const presets = { GFA00: 'PLA Basic', GFA01: 'PLA Matte' };

  beforeEach(() => {
    setColorCatalog({ '0a2989': 'Blue' }, {}, bambu, presets);
  });

  it('names it by the slot range', () => {
    expect(getColorName('5A657BFF', 'PLA Basic')).toBe('Blue Grey');
  });

  it('takes the range from the preset id when the slot has no RFID', () => {
    expect(getColorName('5A657BFF', null, 'GFA00')).toBe('Blue Grey');
    expect(getColorName('5A657BFF', '', 'gfa00')).toBe('Blue Grey');
  });

  it('stays within the slot range when Bambu makes it', () => {
    // One step from PETG Translucent's grey, yet this is a PLA Basic slot.
    expect(getColorName('8E9088FF', 'PLA Basic')).toBe('Gray');
  });

  it('looks in other ranges only for filament outside Bambu ranges', () => {
    expect(getColorName('5A657BFF', 'Some Other PLA')).toBe('Blue Grey');
    expect(getColorName('5A657BFF')).toBe('Blue Grey');
  });

  it('keeps the hue bucket for a colour near nothing', () => {
    expect(getColorName('00FF00FF', 'PLA Basic')).toBe(hexToColorName('00FF00FF'));
  });

  it('an exact catalogue colour still wins', () => {
    expect(getColorName('0A2989FF', 'PLA Basic')).toBe('Blue');
  });

  it('answers as before when the catalogue carries no Bambu rows', () => {
    setColorCatalog({ '0a2989': 'Blue' });
    expect(getColorName('5A657BFF', 'PLA Basic')).toBe(hexToColorName('5A657BFF'));
  });
});
