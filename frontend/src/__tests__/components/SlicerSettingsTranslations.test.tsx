import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import i18n from '../../i18n';
import SlicerSettingsPanel from '../../components/SlicerSettingsPanel';
import type { SettingValue } from '../../types/slicerSettings';
import schema from '../../data/slicer/process-schema.json';
import tree from '../../data/slicer/process-ui-tree.json';
import uk from '../../data/slicer/process-uk.json';

afterEach(async () => { await act(() => i18n.changeLanguage('en')); });

const translations: Record<string, string> = uk;

function Harness({ onChange }: { onChange: ReturnType<typeof vi.fn> }) {
  const [values, setValues] = useState<Record<string, SettingValue>>({});
  return <SlicerSettingsPanel values={values} onChange={(next, serialized) => {
    setValues(next);
    onChange(serialized);
  }} />;
}

describe('OrcaSlicer parameter translations', () => {
  it('covers every visible label, tooltip, enum label, page and group in the vendored metadata', () => {
    for (const option of Object.values(schema)) {
      for (const field of ['label', 'tooltip', 'sidetext'] as const) {
        const text = (option as { label: string; tooltip?: string; sidetext?: string })[field];
        if (text && !text.includes('->') && !text.includes('::')) expect(translations[text], text).toBeTruthy();
      }
      for (const label of (option as { enum_labels?: string[] }).enum_labels ?? []) {
        expect(translations[label], label).toBeTruthy();
      }
    }
    for (const page of tree) {
      expect(translations[page.page], page.page).toBeTruthy();
      for (const group of page.groups) expect(translations[group.group], group.group).toBeTruthy();
    }
  });

  it('renders Ukrainian headings, labels, tooltips, units and enum text while submitting raw enum values', async () => {
    await act(() => i18n.changeLanguage('uk'));
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const layer = await screen.findByLabelText('Висота шару');
    expect(screen.getByRole('button', { name: 'Якість' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Міцність' })).toBeInTheDocument();
    expect(screen.getByText('Ширина лінії')).toBeInTheDocument();
    const row = layer.closest('.group')!;
    expect(row).toHaveAttribute('title', translations[schema.layer_height.tooltip]);
    expect(within(row as HTMLElement).getByText('мм')).toBeInTheDocument();
    const seam = screen.getByLabelText('Розташування шва');
    expect(within(seam).getByRole('option', { name: 'Вирівняне' })).toHaveValue('aligned');
    fireEvent.change(seam, { target: { value: 'random' } });
    expect(onChange).toHaveBeenLastCalledWith({ seam_position: 'random' });
  });

  it('keeps edited values and the same control when switching languages', async () => {
    await act(() => i18n.changeLanguage('en'));
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const layer = await screen.findByLabelText('Layer height');
    fireEvent.change(layer, { target: { value: '0.28' } });
    expect(onChange).toHaveBeenLastCalledWith({ layer_height: '0.28' });
    const calls = onChange.mock.calls.length;
    await act(() => i18n.changeLanguage('uk'));
    expect(screen.getByLabelText('Висота шару')).toBe(layer);
    expect(layer).toHaveValue(0.28);
    await act(() => i18n.changeLanguage('en'));
    expect(screen.getByLabelText('Layer height')).toBe(layer);
    expect(layer).toHaveValue(0.28);
    expect(onChange).toHaveBeenCalledTimes(calls);
  });

  it('searches Ukrainian labels and enum text as well as original English setting keys', async () => {
    await act(() => i18n.changeLanguage('uk'));
    render(<Harness onChange={vi.fn()} />);
    await screen.findByLabelText('Висота шару');
    const search = screen.getByRole('searchbox');
    fireEvent.change(search, { target: { value: 'Висота шару' } });
    expect(screen.getByLabelText('Висота шару')).toBeInTheDocument();
    fireEvent.change(search, { target: { value: 'layer_height' } });
    expect(screen.getByLabelText('Висота шару')).toBeInTheDocument();
    fireEvent.change(search, { target: { value: 'Гіроїд' } });
    await waitFor(() => expect(screen.getByRole('option', { name: 'Гіроїд' })).toHaveValue('gyroid'));
  });
});
