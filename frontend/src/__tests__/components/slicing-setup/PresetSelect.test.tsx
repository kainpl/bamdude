import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { PresetSource, UnifiedPreset, UnifiedPresetsResponse } from '../../../api/client';
import { PresetSelect } from '../../../components/slicing-setup/PresetSelect';
import { buildPresetField } from '../../../components/slicing-setup/presetSets';
import { EMPTY_COMPATIBILITY_INDEX } from '../../../utils/slicerPrinterMatch';

// The family's preset select (R2, R3, R9, R12, §12).
const X1C = 'Bambu Lab X1 Carbon 0.4 nozzle';
const A1 = 'Bambu Lab A1 0.4 nozzle';
const p = (source: PresetSource, id: string, fitsFor: string | null) =>
  ({ source, id, name: id, compatible_printers: fitsFor ? [fitsFor] : null }) as UnifiedPreset;
function listing(process: UnifiedPreset[]): UnifiedPresetsResponse {
  const empty = { printer: [], process: [], filament: [] };
  const by = (s: PresetSource) => ({ ...empty, process: process.filter((x) => x.source === s) });
  return { local: by('local'), orca_cloud: by('orca_cloud'), cloud: by('cloud'), standard: by('standard') } as unknown as UnifiedPresetsResponse;
}
const fit = (mode: 'group' | 'hide') => ({ printerName: X1C, compatIndex: EMPTY_COMPATIBILITY_INDEX, mode });
const values = () => Array.from((screen.getByRole('combobox') as HTMLSelectElement).options).map((o) => o.value);

describe('PresetSelect', () => {
  it('pinned listed once after the display set', () => {
    const field = buildPresetField({
      listing: listing([p('standard', 'fit', X1C), p('standard', 'other', A1)]),
      slot: 'process',
      fit: fit('group'),
      filters: [],
      pinned: { source: 'standard', id: 'other' },
    });
    render(<PresetSelect label="Process" field={field} value={{ source: 'standard', id: 'other' }} onChange={() => {}} />);
    expect(values().filter((v) => v === 'standard:other')).toHaveLength(1);
  });

  it('other-printer note survives the no-fit fallback (§4.4.8)', () => {
    const field = buildPresetField({
      listing: listing([p('standard', 'a1-only', A1)]),
      slot: 'process',
      fit: fit('group'),
      filters: [],
      pinned: { source: 'standard', id: 'a1-only' },
    });
    expect(field.noFit).toBe(true);
    render(<PresetSelect label="Process" field={field} value={{ source: 'standard', id: 'a1-only' }} onChange={() => {}} />);
    const select = screen.getByRole('combobox');
    expect(select).toHaveAccessibleDescription(/another printer/i);
  });

  it('each note described-by its field', () => {
    const field = buildPresetField({
      listing: listing([p('local', 'mine-mismatch', A1), p('standard', 'std-fit', X1C)]),
      slot: 'process',
      fit: fit('group'),
      filters: [{ id: 'source', keep: (x) => x.source === 'local', pickKeep: (x) => x.source === 'local' }],
      pinned: null,
    });
    const onShowAll = vi.fn();
    render(<PresetSelect label="Process" field={field} value={null} onChange={() => {}} gone notProven onShowAll={onShowAll} />);
    const description = screen.getByRole('combobox').getAttribute('aria-describedby') ?? '';
    expect(description.split(' ').length).toBeGreaterThanOrEqual(3);
  });

  it('a relaxed note offers Show all for exactly its filter', async () => {
    const field = buildPresetField({
      listing: listing([p('local', 'mine-mismatch', A1), p('standard', 'std-fit', X1C)]),
      slot: 'process',
      fit: fit('group'),
      filters: [{ id: 'source', keep: (x) => x.source === 'local', pickKeep: (x) => x.source === 'local' }],
      pinned: null,
    });
    const onShowAll = vi.fn();
    render(<PresetSelect label="Process" field={field} value={null} onChange={() => {}} onShowAll={onShowAll} />);
    await userEvent.setup().click(screen.getByRole('button', { name: /show all/i }));
    expect(onShowAll).toHaveBeenCalledWith('source');
  });

  it('search keeps the pinned ref', async () => {
    const field = buildPresetField({
      listing: listing([p('standard', 'alpha', X1C), p('standard', 'beta', X1C)]),
      slot: 'process',
      fit: fit('group'),
      filters: [],
      pinned: { source: 'standard', id: 'beta' },
    });
    render(<PresetSelect label="Process" field={field} value={{ source: 'standard', id: 'beta' }} onChange={() => {}} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /process/i }));
    await user.type(screen.getByRole('searchbox'), 'alpha');
    expect(values()).toContain('standard:beta');
  });

  it('colour has its own label', async () => {
    const field = buildPresetField({ listing: listing([p('standard', 'a', X1C)]), slot: 'process', filters: [], pinned: null });
    const onColour = vi.fn();
    render(
      <PresetSelect
        label="Filament 1"
        field={field}
        value={null}
        onChange={() => {}}
        colour={{ value: '#FF0000', onChange: onColour, label: 'Filament colour' }}
      />,
    );
    expect(screen.getByLabelText('Filament colour')).toHaveAttribute('type', 'color');
  });

  it('unique ids for two instances', () => {
    const field = buildPresetField({ listing: listing([p('standard', 'a', X1C)]), slot: 'process', filters: [], pinned: null });
    render(
      <>
        <PresetSelect label="One" field={field} value={null} onChange={() => {}} />
        <PresetSelect label="Two" field={field} value={null} onChange={() => {}} />
      </>,
    );
    const ids = screen.getAllByRole('combobox').map((el) => el.id);
    expect(new Set(ids).size).toBe(2);
  });

  it('boundFallback shows everything enabled; an empty bound alone disables', () => {
    const data = listing([p('standard', 'a', X1C)]);
    const fallback = buildPresetField({ listing: data, slot: 'process', bound: { keep: () => false, whenEmpty: 'showAll' }, filters: [], pinned: null });
    const { rerender } = render(<PresetSelect label="Printer" field={fallback} value={null} onChange={() => {}} />);
    expect(screen.getByRole('combobox')).toBeEnabled();
    const empty = buildPresetField({ listing: data, slot: 'process', bound: { keep: () => false, whenEmpty: 'disable' }, filters: [], pinned: null });
    rerender(<PresetSelect label="Printer" field={empty} value={null} onChange={() => {}} />);
    expect(screen.getByRole('combobox')).toBeDisabled();
  });

  it('keyboard: the select is reachable and changes the value', async () => {
    const field = buildPresetField({ listing: listing([p('standard', 'a', X1C), p('standard', 'b', X1C)]), slot: 'process', filters: [], pinned: null });
    const onChange = vi.fn();
    render(<PresetSelect label="Process" field={field} value={null} onChange={onChange} />);
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole('combobox'), 'standard:b');
    expect(onChange).toHaveBeenCalledWith({ source: 'standard', id: 'b' });
    expect(within(screen.getByRole('combobox')).getAllByRole('option').length).toBeGreaterThan(2);
  });
});
