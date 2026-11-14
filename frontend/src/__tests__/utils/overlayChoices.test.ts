import { describe, it, expect } from 'vitest';
import {
  DEFAULT_OVERLAY_CHOICES,
  overlayChoices,
  overlayDesign,
  overlayLayout,
  overlayTransparency,
  parseOverlayUrl,
} from '../../utils/overlayOptions';

describe('overlayDesign / overlayLayout / overlayTransparency', () => {
  it('reads only the values the overlay knows', () => {
    expect(overlayDesign('2')).toBe('v2');
    expect(['1', 'classic', '', null, undefined, '22', ' 2', 'v2'].map(overlayDesign)).toEqual(Array(8).fill('classic'));

    expect(overlayLayout('landscape')).toBe('landscape');
    expect(overlayLayout('portrait')).toBe('portrait');
    expect(['', null, undefined, 'Portrait', 'both', 'auto', 'portrait '].map(overlayLayout)).toEqual(Array(7).fill(null));

    expect([overlayTransparency('0'), overlayTransparency('40'), overlayTransparency('100')]).toEqual([0, 40, 100]);
    expect(['101', '-1', '1e1', '4.5', ' 4', '', null, undefined, '0x1f', '٤٠'].map(overlayTransparency)).toEqual(
      Array(10).fill(0),
    );
  });
});

describe('overlayChoices', () => {
  it('is the defaults for anything that is not an object', () => {
    for (const raw of [null, undefined, 'text', 7, true]) {
      expect(overlayChoices(raw)).toEqual(DEFAULT_OVERLAY_CHOICES);
    }
  });

  it('keeps what the builder could have written', () => {
    const stored = {
      printerId: 3,
      fields: ['printer', 'model', 'nozzle'],
      size: 'small',
      fps: 5,
      showCamera: false,
      customColours: true,
      progressFrom: '#ff8800',
      progressTo: '#0088ff',
      design: '2',
      transparency: 35,
      layout: 'both',
      showLogo: true,
    };
    expect(overlayChoices(stored)).toEqual(stored);
  });

  it('puts fields in the overlay\'s order and drops the unknown ones', () => {
    expect(overlayChoices({ fields: ['eta', 'printer', 'nope', 3, 'model'] }).fields).toEqual(['printer', 'model', 'eta']);
    // Nothing chosen is a choice too.
    expect(overlayChoices({ fields: [] }).fields).toEqual([]);
  });

  it('has no place for a token', () => {
    expect(overlayChoices({ token: 'bblt_secret', size: 'large' })).not.toHaveProperty('token');
    expect(JSON.stringify(overlayChoices({ token: 'bblt_secret' }))).not.toContain('bblt_secret');
  });

  it.each([
    [{ printerId: 0 }, 'printerId', null],
    [{ printerId: -4 }, 'printerId', null],
    [{ printerId: 2.5 }, 'printerId', null],
    [{ printerId: '2' }, 'printerId', null],
    [{ fps: 0 }, 'fps', 15],
    [{ fps: 31 }, 'fps', 15],
    [{ fps: Number.NaN }, 'fps', 15],
    [{ transparency: 101 }, 'transparency', 0],
    [{ transparency: -1 }, 'transparency', 0],
    [{ design: 2 }, 'design', 'classic'],
    [{ layout: 'sideways' }, 'layout', 'auto'],
    [{ size: 'x' }, 'size', 'medium'],
    [{ customColours: true, progressFrom: 'red', progressTo: '#0000ff' }, 'customColours', false],
    [{ customColours: 'yes', progressFrom: '#ff0000', progressTo: '#0000ff' }, 'customColours', false],
  ] as const)('falls back for %j', (raw, key, expected) => {
    expect(overlayChoices(raw)[key]).toBe(expected);
  });
});

describe('parseOverlayUrl', () => {
  it('reads the printer, the choices and the token, whatever the origin', () => {
    const parsed = parseOverlayUrl(
      'https://elsewhere.example/overlay/12?show=printer,progress&size=small&fps=3&camera=false&design=2'
      + '&backgroundTransparency=20&layout=landscape&progressFrom=112233&progressTo=%23445566&token=bblt_x',
    );
    expect(parsed).toEqual({
      token: 'bblt_x',
      choices: {
        printerId: 12,
        fields: ['printer', 'progress'],
        size: 'small',
        fps: 3,
        showCamera: false,
        customColours: true,
        progressFrom: '#112233',
        progressTo: '#445566',
        design: '2',
        transparency: 20,
        layout: 'landscape',
        showLogo: false,
      },
    });
    // The origin is nowhere in what comes back.
    expect(JSON.stringify(parsed)).not.toContain('elsewhere');
  });

  it('reads a bare address as the overlay would draw it', () => {
    expect(parseOverlayUrl('http://farm.local/overlay/1')).toEqual({
      token: '',
      choices: { ...DEFAULT_OVERLAY_CHOICES, printerId: 1 },
    });
    // An empty `show` is "nothing", not "the default set".
    expect(parseOverlayUrl('/overlay/1?show=')?.choices.fields).toEqual([]);
    // One colour alone is a plain bar.
    expect(parseOverlayUrl('/overlay/1?progressTo=0000ff')?.choices).toMatchObject({
      customColours: true,
      progressFrom: '#0000ff',
      progressTo: '#0000ff',
    });
  });

  it.each([
    '',
    'overlay',
    'ftp://farm.local/overlay/1',
    'javascript:alert(1)',
    'https://user@farm.local/overlay/1',
    'https://farm.local/overlay/',
    'https://farm.local/overlay/01',
    'https://farm.local/overlay/1.5',
    'https://farm.local/overlay/12345678901',
    'https://farm.local/api/v1/overlay/1',
    'https://farm.local/overlay/1?token=a&token=b',
  ])('refuses %j', (text) => {
    expect(parseOverlayUrl(text)).toBeNull();
  });
});

describe('the logo choice', () => {
  it('is on only for logo=1', async () => {
    const { overlayLogo } = await import('../../utils/overlayOptions');
    expect(overlayLogo('1')).toBe(true);
    expect(['', null, undefined, '0', 'true', 'yes', '11', ' 1', '1 ', '01'].map(overlayLogo)).toEqual(Array(10).fill(false));
  });

  it('is remembered only as a plain yes', () => {
    expect(overlayChoices({ showLogo: true }).showLogo).toBe(true);
    expect([false, 'true', 1, null, undefined].map((showLogo) => overlayChoices({ showLogo }).showLogo)).toEqual(Array(5).fill(false));
    expect(DEFAULT_OVERLAY_CHOICES.showLogo).toBe(false);
  });

  it('is read off an address', () => {
    expect(parseOverlayUrl('https://farm.example/overlay/3?logo=1')?.choices.showLogo).toBe(true);
    expect(parseOverlayUrl('https://farm.example/overlay/3?logo=yes')?.choices.showLogo).toBe(false);
    expect(parseOverlayUrl('https://farm.example/overlay/3')?.choices.showLogo).toBe(false);
  });
});
