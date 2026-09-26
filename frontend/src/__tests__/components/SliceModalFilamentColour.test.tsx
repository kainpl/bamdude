/**
 * The slice dialog records the colour each slot is printed in (upstream
 * 4f10d155, #2977) — wiring, read off the source like the other SliceModal
 * checks. The decisions themselves are in `utils/sliceFilamentColours` and are
 * tested there.
 *
 * ⚠️ A recorded colour is not a requirement: dispatch ranks by colour and
 * refuses nothing unless the job forces colour matching, so printing in
 * another colour stays an ordinary choice. What this fixes is the file saying
 * #00AE42 whatever was picked.
 */
import { describe, expect, it } from 'vitest';

import modalSource from '../../components/SliceModal.tsx?raw';
import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';

describe('SliceModal — filament colour per slot', () => {
  it('sends one colour per slot through the helper', () => {
    expect(modalSource).toContain('filament_colours: filamentColoursPayload(filamentSlots, filamentColours)');
  });

  it('drops the picks when the plate layout changes', () => {
    expect(modalSource).toContain('resizeColourOverrides(current, filamentSlots.length)');
  });

  it('gives every filament row its colour control, single-slot sources included', () => {
    // An STL is exactly the source with no colour anywhere else to inherit.
    const rows = modalSource.slice(modalSource.indexOf('filamentSlots.map((slot, idx) =>'));
    const dropdown = rows.slice(0, rows.indexOf('/>'));
    expect(dropdown).toContain('onSwatchColorChange=');
    expect(dropdown).toContain("swatchColorLabel={t('slice.filamentColour')}");
    expect(dropdown).not.toContain('filamentSlots.length > 1 ? slot.color');
  });

  it('names the control in both locales', () => {
    expect((en as { slice: Record<string, unknown> }).slice.filamentColour).toBeTruthy();
    expect((uk as { slice: Record<string, unknown> }).slice.filamentColour).toBeTruthy();
  });
});
