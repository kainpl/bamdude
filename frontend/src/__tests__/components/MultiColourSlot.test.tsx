/**
 * A spool with several colours, drawn and named as several colours.
 *
 * Registry N5. The dot used to take one hex and paint the circle with it, so a
 * two-colour spool looked like whichever colour firmware listed first — and,
 * worse, was NAMED after it. "Black" on a black-and-white spool is not a rough
 * label; it is one somebody will pick for a black print.
 *
 * ⚠️ The drawing rule is BambuStudio's and contradicts its own constant names
 * (`AMSItem.cpp`): `CTYPE_MULTI` (0) blends first→last, everything else with
 * more than one colour draws equal bands. Copied as found.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { FilamentSlotCircle } from '../../components/FilamentSlotCircle';
import { resolveMultiColorName } from '../../utils/colors';

function circleOf(container: HTMLElement): HTMLElement {
  return container.firstElementChild as HTMLElement;
}

describe('FilamentSlotCircle — several colours', () => {
  it('bands a two-colour spool into equal sectors', () => {
    const { container } = render(
      <FilamentSlotCircle trayColor="FF0000" trayColors={['FF0000', '00FF00']} ctype={2} isEmpty={false} slotNumber={1} />,
    );

    const style = circleOf(container).style.backgroundImage;
    expect(style).toContain('conic-gradient');
    // Equal halves: 0-180 and 180-360.
    expect(style).toContain('180deg');
  });

  it('blends when the printer says MULTI, because that is what Studio does', () => {
    const { container } = render(
      <FilamentSlotCircle trayColor="FF0000" trayColors={['FF0000', '0000FF']} ctype={0} isEmpty={false} slotNumber={1} />,
    );

    const style = circleOf(container).style.backgroundImage;
    expect(style).toContain('linear-gradient');
    expect(style).not.toContain('conic-gradient');
  });

  it('leaves a single-colour spool exactly as it was', () => {
    const { container } = render(
      <FilamentSlotCircle trayColor="FF0000" trayColors={['FF0000']} ctype={2} isEmpty={false} slotNumber={1} />,
    );

    expect(circleOf(container).style.backgroundImage).toBe('');
  });

  it('does not paint an empty slot', () => {
    const { container } = render(
      <FilamentSlotCircle trayColors={['FF0000', '00FF00']} ctype={2} isEmpty slotNumber={1} />,
    );

    expect(circleOf(container).style.backgroundImage).toBe('');
  });

  it('keeps working for the call sites that pass no list at all', () => {
    // Fifteen of them existed before this feature; the prop had to stay optional.
    const { container } = render(<FilamentSlotCircle trayColor="FF0000" isEmpty={false} slotNumber={3} />);

    expect(screen.getByText('3')).toBeInTheDocument();
    expect(circleOf(container).style.backgroundImage).toBe('');
  });

  it('judges the digit contrast on the average, not the first colour', () => {
    // ⚠️ Black + white averages to mid-grey. Picking by the first colour would
    // make the same spool render a white or a black digit depending only on
    // which end firmware happened to list first.
    const { container: blackFirst } = render(
      <FilamentSlotCircle trayColors={['000000', 'FFFFFF']} ctype={2} isEmpty={false} slotNumber={1} />,
    );
    const { container: whiteFirst } = render(
      <FilamentSlotCircle trayColors={['FFFFFF', '000000']} ctype={2} isEmpty={false} slotNumber={1} />,
    );

    const digit = (c: HTMLElement) => (c.querySelector('span') as HTMLElement).style.color;
    expect(digit(blackFirst)).toBe(digit(whiteFirst));
  });
});

describe('resolveMultiColorName', () => {
  it('names every colour rather than pretending there is one', () => {
    const name = resolveMultiColorName(['000000', 'FFFFFF']);

    expect(name).toBeTruthy();
    expect(name).toContain('+');
  });

  it('says a repeated name once', () => {
    const name = resolveMultiColorName(['000000', '000000']);

    expect(name).not.toContain('+');
  });

  it('stops listing after three and counts the rest', () => {
    const name = resolveMultiColorName(['FF0000', '00FF00', '0000FF', 'FFFF00', 'FF00FF']);

    expect(name).toMatch(/\+\d$/);
  });

  it('returns null for nothing, so callers keep their placeholder', () => {
    expect(resolveMultiColorName([])).toBeNull();
    expect(resolveMultiColorName(null)).toBeNull();
    expect(resolveMultiColorName(undefined)).toBeNull();
  });
});
