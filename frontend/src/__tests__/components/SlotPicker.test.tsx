/**
 * The slot picker of the print dialog (upstream #3159, #3294): every slot with
 * a swatch of the spool's colour and its hex, the exact match marked, usable
 * from the keyboard and on a phone, and a popover — not a modal — whose Escape
 * closes the list alone.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within } from '@testing-library/react';
import { SlotPicker, type SlotOption } from '../../components/PrintModal/SlotPicker';
import { displayHex } from '../../utils/displayHex';
import { slotSpoolSwatches } from '../../utils/slotSpoolNames';
import type { InventorySpool } from '../../api/client';

const OPTIONS: SlotOption[] = [
  { value: 0, slot: 'A1', name: 'Acme PLA Red', color: '#FF0000', detail: ' - 800g left' },
  {
    value: 1, slot: 'A2', name: 'Acme PLA Sunset', color: 'FF8800FF', extraColors: 'FF0000,FFFF00',
    effectType: 'silk', exact: true,
  },
  { value: 2, slot: 'A3', name: 'Clear PETG', color: 'FFFFFF80' },
  { value: 254, slot: 'Ext', name: 'TPU', color: '000000', disabled: true, disabledReason: 'not through the switch' },
];

function setup(value: number | null = 0, onChange = vi.fn()) {
  const view = render(
    <SlotPicker
      value={value}
      options={OPTIONS}
      onChange={onChange}
      placeholder="-- Select slot --"
      toneClass="border-bambu-green/50"
      title="Auto-matched"
      ariaLabel="Slot for PLA"
      exactLabel="Same material and colour as the file asks for"
    />,
  );
  const button = screen.getByRole('button', { name: 'Slot for PLA' });
  return { ...view, button, onChange };
}

afterEach(cleanup);

describe('displayHex', () => {
  it('drops an opaque alpha and keeps a translucent one', () => {
    expect(displayHex('#ff0000')).toBe('#FF0000');
    expect(displayHex('FF0000FF')).toBe('#FF0000');
    expect(displayHex('FFFFFF80')).toBe('#FFFFFF80');
    expect(displayHex('')).toBe('');
    expect(displayHex(null)).toBe('');
    expect(displayHex('nonsense')).toBe('');
  });
});

describe('SlotPicker', () => {
  it('the row shows the chosen slot with its swatch and hex', () => {
    const { button } = setup(0);
    expect(button).toHaveTextContent('A1: Acme PLA Red');
    expect(button).toHaveTextContent('#FF0000');
    expect(within(button).getByTestId('filament-swatch')).toBeInTheDocument();
    expect(button).toHaveAttribute('aria-expanded', 'false');
  });

  it('with nothing chosen the row shows the placeholder and no swatch', () => {
    const { button } = setup(null);
    expect(button).toHaveTextContent('-- Select slot --');
    expect(within(button).queryByTestId('filament-swatch')).toBeNull();
  });

  it('the open list gives every slot a swatch, its hex and its text — and marks the exact match', () => {
    const { button } = setup(0);
    fireEvent.click(button);
    const list = screen.getByRole('listbox', { name: 'Slot for PLA' });
    const entries = within(list).getAllByRole('option');
    expect(entries).toHaveLength(5); // "no slot" + four slots
    expect(entries[1]).toHaveTextContent('A1: Acme PLA Red - 800g left');
    expect(entries[1]).toHaveTextContent('#FF0000');
    expect(entries[3]).toHaveTextContent('#FFFFFF80');
    for (const entry of entries.slice(1)) expect(within(entry).getByTestId('filament-swatch')).toBeInTheDocument();
    // A multi-colour spool is drawn with all its colours, not its base alone.
    expect(within(entries[2]).getByTestId('filament-swatch').getAttribute('title')).toMatch(/FF0000.*FFFF00/i);
    expect(within(entries[2]).getByLabelText('Same material and colour as the file asks for')).toBeInTheDocument();
    expect(within(entries[1]).queryByLabelText('Same material and colour as the file asks for')).toBeNull();
    expect(entries[1]).toHaveAttribute('aria-selected', 'true');
  });

  it('a click chooses a slot, and the first entry clears the choice', () => {
    const { button, onChange } = setup(0);
    fireEvent.click(button);
    fireEvent.click(screen.getByRole('option', { name: /A2: Acme PLA Sunset/ }));
    expect(onChange).toHaveBeenCalledWith(1);
    expect(button).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(button);
    fireEvent.click(screen.getByRole('option', { name: '-- Select slot --' }));
    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it('a slot that cannot be used is shown with its reason and cannot be chosen', () => {
    const { button, onChange } = setup(0);
    fireEvent.click(button);
    const external = screen.getByRole('option', { name: /Ext: TPU/ });
    expect(external).toHaveAttribute('aria-disabled', 'true');
    expect(external).toHaveTextContent('not through the switch');
    fireEvent.click(external);
    expect(onChange).not.toHaveBeenCalled();
    expect(button).toHaveAttribute('aria-expanded', 'true');
  });

  it('works from the keyboard: arrows move, Enter chooses, disabled entries are skipped', () => {
    const { button, onChange } = setup(0);
    fireEvent.keyDown(button, { key: 'ArrowDown' });
    expect(button).toHaveAttribute('aria-expanded', 'true');
    const active = () => document.getElementById(button.getAttribute('aria-activedescendant')!)!;
    expect(active()).toHaveTextContent('A1: Acme PLA Red');
    fireEvent.keyDown(button, { key: 'ArrowDown' });
    expect(active()).toHaveTextContent('A2: Acme PLA Sunset');
    fireEvent.keyDown(button, { key: 'End' });
    expect(active()).toHaveTextContent('A3: Clear PETG'); // the disabled Ext is not a stop
    fireEvent.keyDown(button, { key: 'ArrowDown' });
    expect(active()).toHaveTextContent('-- Select slot --'); // wraps past the disabled entry
    fireEvent.keyDown(button, { key: 'ArrowUp' });
    fireEvent.keyDown(button, { key: 'ArrowUp' });
    expect(active()).toHaveTextContent('A2: Acme PLA Sunset');
    fireEvent.keyDown(button, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith(1);
    expect(button).toHaveAttribute('aria-expanded', 'false');
  });

  it('Escape closes the list only — the dialog under it never sees the key', () => {
    const { button, onChange } = setup(0);
    const dialogEscape = vi.fn(); // modalStack listens on window
    window.addEventListener('keydown', dialogEscape);
    try {
      fireEvent.click(button);
      fireEvent.keyDown(button, { key: 'Escape' });
      expect(button).toHaveAttribute('aria-expanded', 'false');
      expect(dialogEscape).not.toHaveBeenCalled();
      expect(onChange).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(button);
      // Closed, the key is the dialog's again.
      fireEvent.keyDown(button, { key: 'Escape' });
      expect(dialogEscape).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener('keydown', dialogEscape);
    }
  });

  it('a click outside closes it', () => {
    const { button } = setup(0);
    fireEvent.click(button);
    fireEvent.mouseDown(document.body);
    expect(button).toHaveAttribute('aria-expanded', 'false');
  });

  it('is a popover inside its own row, sized for a phone: not portalled, 44 px entries, never wider than the row there', () => {
    const { button, container } = setup(0);
    fireEvent.click(button);
    const list = screen.getByRole('listbox');
    expect(container.contains(list)).toBe(true);
    expect(list.parentElement).toBe(button.parentElement);
    expect(list.className).not.toMatch(/\bfixed\b/);
    expect(list).toHaveClass('max-sm:w-full', 'max-sm:max-w-full', 'absolute');
    for (const entry of within(list).getAllByRole('option')) expect(entry).toHaveClass('min-h-[44px]');
  });
});

describe('slotSpoolSwatches', () => {
  const spool = (id: number, extra: Partial<InventorySpool>) =>
    ({ id, material: 'PLA', label_weight: 1000, weight_used: 0, rgba: 'FF8800FF', ...extra }) as InventorySpool;

  it('draws a bound spool as itself in either inventory, and leaves an unbound slot to the tray', () => {
    const builtIn = slotSpoolSwatches({
      spoolmanMode: false,
      assignments: [
        { ams_id: 0, tray_id: 1, spool: spool(1, { extra_colors: 'FF0000,FFFF00', effect_type: 'silk', subtype: 'Silk' }) },
        { ams_id: 0, tray_id: 2, spool: null },
      ],
    });
    expect(builtIn.get(1)).toEqual({ rgba: 'FF8800FF', extraColors: 'FF0000,FFFF00', effectType: 'silk', subtype: 'Silk' });
    expect(builtIn.has(2)).toBe(false);

    const spoolman = slotSpoolSwatches({
      spoolmanMode: true,
      spoolmanAssignments: [{ ams_id: 0, tray_id: 0, spoolman_spool_id: 7 }],
      spoolmanSpools: [spool(7, { rgba: '112233FF', extra_colors: '112233,445566' })],
    });
    expect(spoolman.get(0)?.extraColors).toBe('112233,445566');
  });
});
