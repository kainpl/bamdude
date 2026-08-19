/**
 * A count per plate, not one count for all of them.
 *
 * One shared Quantity field cannot say "plate 1 once, plate 2 twice" — the
 * only answer was to submit each plate separately and keep the counts in your
 * head (upstream #342).
 *
 * ⚠️ The stepper appears only once MORE THAN ONE plate is selected. With a
 * single plate the dialog's shared Quantity field already answers the same
 * question, and two controls for one number are two sources of truth.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';

import { PlateSelector } from '../../components/PrintModal/PlateSelector';
import type { PlateInfo } from '../../components/PrintModal/types';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (_key: string, fallback?: string) => fallback ?? _key }),
}));

function plate(index: number): PlateInfo {
  return {
    index,
    name: `Plate ${index}`,
    has_thumbnail: false,
    thumbnail_url: null,
    print_time_seconds: null,
    filament_used_grams: null,
    object_count: 0,
    objects: [],
    filaments: [],
  } as unknown as PlateInfo;
}

const PLATES = [plate(1), plate(2), plate(3)];

function renderSelector(overrides: Partial<React.ComponentProps<typeof PlateSelector>> = {}) {
  const onQuantityChange = vi.fn();
  render(
    <PlateSelector
      plates={PLATES}
      isMultiPlate
      selectedPlates={new Set([1, 2])}
      onToggle={vi.fn()}
      multiSelect
      quantities={{ 1: 1, 2: 1 }}
      onQuantityChange={onQuantityChange}
      {...overrides}
    />,
  );
  return { onQuantityChange };
}

describe('per-plate copies', () => {
  it('offers a count for the plate on screen', () => {
    renderSelector();
    expect(screen.getByText('Copies of this plate')).toBeInTheDocument();
  });

  it('reports the plate it belongs to, not just the number', async () => {
    const { onQuantityChange } = renderSelector({ selectedPlates: new Set([2, 3]), quantities: { 2: 1, 3: 1 } });

    await userEvent.click(screen.getByLabelText('More'));

    expect(onQuantityChange).toHaveBeenCalledWith(2, 2);
  });

  it('stops at one — zero copies of a plate is not a request, it is a deselection', async () => {
    const { onQuantityChange } = renderSelector();

    expect(screen.getByLabelText('Fewer')).toBeDisabled();
    await userEvent.click(screen.getByLabelText('Fewer'));
    expect(onQuantityChange).not.toHaveBeenCalled();
  });

  it('stops at fifty, the same ceiling the shared field has', async () => {
    renderSelector({ quantities: { 1: 50, 2: 1 } });

    expect(screen.getByLabelText('More')).toBeDisabled();
  });

  it('stays hidden for a single selected plate', () => {
    renderSelector({ selectedPlates: new Set([1]), quantities: { 1: 1 } });

    expect(screen.queryByText('Copies of this plate')).not.toBeInTheDocument();
  });

  it('stays hidden where per-plate counts make no sense', () => {
    renderSelector({ onQuantityChange: undefined });

    expect(screen.queryByText('Copies of this plate')).not.toBeInTheDocument();
  });

});
