import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useState } from 'react';
import i18n from '../../i18n';
import SlicerSettingsPanel from '../../components/SlicerSettingsPanel';
import type { SettingValue } from '../../types/slicerSettings';

vi.mock('../../data/slicer/process-uk.json', () => { throw new Error('optional chunk unavailable'); });
afterEach(async () => { await act(() => i18n.changeLanguage('en')); });

it.each(['en', 'uk'])('edits the form in %s when the optional dictionary rejects', async (language) => {
  await act(() => i18n.changeLanguage(language));
  const changed = vi.fn();
  function Harness() {
    const [values, setValues] = useState<Record<string, SettingValue>>({});
    return <SlicerSettingsPanel values={values} onChange={(next, serialized) => { setValues(next); changed(serialized); }} />;
  }
  render(<Harness />);
  const layer = await screen.findByLabelText('Layer height');
  fireEvent.change(layer, { target: { value: '0.28' } });
  expect(layer).toHaveValue(0.28);
  expect(changed).toHaveBeenLastCalledWith({ layer_height: '0.28' });
});
