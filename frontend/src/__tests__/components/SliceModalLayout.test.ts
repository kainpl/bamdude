/**
 * Two columns from `lg`, one stack below it.
 *
 * ⚠️ The panel is 348 options. In a single column it either sits collapsed and
 * unseen, or sits open and pushes everything else off screen. Giving it its
 * own column is the only arrangement where both halves are usable.
 *
 * ⚠️ In embedded mode the right column stays on screen DISABLED rather than
 * disappearing: nothing from it is sent on that path, but removing it made the
 * dialog look like it had lost a feature whenever the toggle flipped.
 *
 * Asserted on the source: jsdom does no layout, so it cannot be asked which
 * width is in effect. The real check is three widths in a browser.
 */
import { describe, it, expect } from 'vitest';

import modalSource from '../../components/SliceModal.tsx?raw';

const sourceLines = modalSource.split(/\r?\n/);
const gridLine = sourceLines.find((line) => line.includes('lg:grid-cols-[minmax(0,20rem)'));

describe('SliceModal layout', () => {
  it('puts the body in a two-column grid', () => {
    expect(gridLine, 'the two-column grid').toBeDefined();
    expect(gridLine).toContain('lg:gap-5');
    expect(gridLine).toContain('lg:items-start');
  });

  it('collapses to one stack below lg', () => {
    // ⚠️ Every grid class is `lg:`-prefixed, so the narrow layout is the one
    // this dialog already had and nothing on a phone changes. An unprefixed
    // `grid` here would apply the two columns at every width.
    expect(gridLine).not.toMatch(/(^|\s)grid(\s|"|$)/);
    expect(gridLine).not.toMatch(/(^|\s)grid-cols-/);
  });

  it('widens the frame at the same breakpoint the grid appears', () => {
    // ⚠️ The miss the first attempt made: the grid went in and the frame stayed
    // at the single-column `max-w-xl` (36rem). A 20rem left column plus the gap
    // left the 348-option panel about 13rem — worse than the one column it
    // replaced. The two must switch together or the layout is a downgrade.
    const frame = sourceLines.find((line) =>
      line.includes('max-h-[85vh] flex flex-col rounded-lg'),
    );

    expect(frame, 'the modal frame').toBeDefined();
    expect(frame).toContain('max-w-xl');
    expect(frame).toContain('lg:max-w-5xl');
  });

  it('keeps the settings panel open and its toggle inert when it owns a column', () => {
    // The behavioural half — the reason useIsWideLayout exists at all.
    expect(modalSource).toContain('const panelOpen = isWideLayout || settingsExpanded;');
    expect(modalSource).toContain('disabled={isWideLayout}');
    expect(modalSource).toContain('aria-expanded={panelOpen}');
  });

  it('keeps the right column on screen in embedded mode, disabled', () => {
    // ⚠️ Not removed: dropping it made the dialog look like it had lost a
    // feature every time the embedded toggle flipped.
    expect(modalSource).toContain("useEmbedded ? 'opacity-60' : ''");
    // ...but nothing from it is sent on that path, so the panel itself stays
    // unrendered there.
    expect(modalSource).toContain('{panelOpen && !useEmbedded && (');
  });

  it('leaves the plate selector above the grid, not inside a column', () => {
    // ⚠️ Ours is inline and non-blocking; upstream's equivalent is a step in a
    // modal above the modal that holds the preset query back while it is open.
    const plateAt = modalSource.indexOf('<SlicePlateSelector');
    const gridAt = modalSource.indexOf('lg:grid-cols-[minmax(0,20rem)');
    expect(plateAt).toBeGreaterThan(-1);
    expect(plateAt).toBeLessThan(gridAt);
  });
});
