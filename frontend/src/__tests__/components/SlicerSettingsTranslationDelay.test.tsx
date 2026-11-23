import { act, fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { useState } from 'react';
import i18n from '../../i18n';
import SlicerSettingsPanel from '../../components/SlicerSettingsPanel';
import type { SettingValue } from '../../types/slicerSettings';

const catalog = vi.hoisted(() => {
  let resolve!: (value: { default: Record<string, string> }) => void;
  const promise = new Promise<{ default: Record<string, string> }>((done) => { resolve = done; });
  return { promise, resolve };
});
vi.mock('../../data/slicer/process-uk.json', () => catalog.promise);

it('keeps editing while the optional catalog loads and across language switches', async () => {
  await act(() => i18n.changeLanguage('en'));
  const changed = vi.fn();
  function Harness() {
    const [values, setValues] = useState<Record<string, SettingValue>>({});
    return <SlicerSettingsPanel values={values} onChange={(next, serialized) => { setValues(next); changed(serialized); }} />;
  }
  render(<Harness />);
  const layer = await screen.findByLabelText('Layer height');
  fireEvent.change(layer, { target: { value: '0.28' } });
  await act(() => i18n.changeLanguage('uk'));
  expect(screen.getByLabelText(/^Layer height/)).toBe(layer);
  fireEvent.change(layer, { target: { value: '0.24' } });
  await act(async () => { catalog.resolve({ default: { 'Layer height': 'Висота шару' } }); });
  expect(await screen.findByLabelText(/^Висота шару/)).toBe(layer);
  expect(layer).toHaveValue(0.24);
  await act(() => i18n.changeLanguage('en'));
  expect(screen.getByLabelText(/^Layer height/)).toBe(layer);
  expect(layer).toHaveValue(0.24);
  expect(changed).toHaveBeenCalledTimes(2);
});
