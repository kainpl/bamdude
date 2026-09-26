/**
 * The colour each filament slot is sliced in (upstream 4f10d155, #2977).
 *
 * Neither slicer stores a colour on a filament preset, so without one on the
 * request every sliced file recorded the CLI's own #00AE42 — a green plate
 * thumbnail and a false "colour mismatch" in the print dialog. The dialog sends
 * a colour per slot; these helpers decide what that colour is.
 */
import { describe, expect, it } from 'vitest';

import {
  SLICER_DEFAULT_COLOUR,
  colourInputValue,
  filamentColoursPayload,
  resizeColourOverrides,
} from '../../utils/sliceFilamentColours';

describe('filamentColoursPayload — what the request carries per slot', () => {
  it("sends the user's pick first", () => {
    expect(filamentColoursPayload([{ color: '#112233' }], ['#AABBCC'])).toEqual(['#AABBCC']);
  });

  it("falls back to the colour the source plate was designed with", () => {
    expect(filamentColoursPayload([{ color: '#112233' }], [null])).toEqual(['#112233']);
  });

  it('sends an EMPTY string for a slot with no colour anywhere', () => {
    // Not the swatch's displayed default: a sent colour outranks the preset's
    // own default_filament_colour, so pinning the placeholder would discard
    // the real colour of an imported OrcaSlicer profile that carries one.
    expect(filamentColoursPayload([{ color: undefined }, { color: '' }], [])).toEqual(['', '']);
  });

  it('keeps an 8-digit AMS colour as it came', () => {
    expect(filamentColoursPayload([{ color: '#11223344' }], [])).toEqual(['#11223344']);
  });

  it('follows the slot order', () => {
    expect(filamentColoursPayload([{ color: '#000001' }, { color: '#000002' }], [null, '#FFFFFF'])).toEqual([
      '#000001',
      '#FFFFFF',
    ]);
  });
});

describe('resizeColourOverrides — a plate switch renumbers the slots', () => {
  it('keeps the picks when the slot count is unchanged (a preset re-pick)', () => {
    const current = ['#AABBCC', null];
    expect(resizeColourOverrides(current, 2)).toBe(current);
  });

  it('drops them when the count changes, so slot 2 of one plate never paints slot 2 of another', () => {
    expect(resizeColourOverrides(['#AABBCC', null], 3)).toEqual([null, null, null]);
  });
});

describe('colourInputValue — what the native colour input can show', () => {
  it('trims the alpha byte for display only', () => {
    expect(colourInputValue('#aabbccdd')).toBe('#AABBCC');
  });

  it("shows the slicer's own default for a slot with no colour, which is what the slice will record", () => {
    expect(colourInputValue(undefined)).toBe(SLICER_DEFAULT_COLOUR);
    expect(colourInputValue('not a colour')).toBe(SLICER_DEFAULT_COLOUR);
  });
});
