/**
 * The field chips of the label editor speak the reader's language.
 *
 * The vocabulary is the server's — which keys exist, and an example for each —
 * but its labels arrived in English only, so a Ukrainian editor offered
 * "Storage location" beside Ukrainian buttons. The chip now looks its key up in
 * the locale and keeps the server's wording only for a key the locale does not
 * know yet.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';

import { render } from '../utils';
import { ElementInspector } from '../../components/labels/ElementInspector';
import { api, type LabelTemplateElement } from '../../api/client';
import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';

const TEXT = {
  type: 'text',
  x_mm: 1,
  y_mm: 1,
  w_mm: 20,
  h_mm: 4,
  content: '{brand}',
  size_mm: 3,
  bold: false,
  italic: false,
  align: 'left',
  valign: 'top',
  fit: 'shrink',
} as unknown as LabelTemplateElement;

const served = (key: string, label: string) => ({ key, label, description: `${label} described`, example: 'x' });

describe('the label editor\'s field chips', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  const show = async (placeholders: unknown[]) => {
    vi.spyOn(api, 'getLabelPlaceholders').mockResolvedValue(placeholders as never);
    render(<ElementInspector element={TEXT} widthMm={40} heightMm={20} onChange={vi.fn()} onDelete={vi.fn()} />);
    await screen.findByText('Insert a field');
  };

  it('names a field from the locale, not from the server\'s English', async () => {
    await show([served('storage_location', 'SERVER WORDING')]);

    expect(await screen.findByRole('button', { name: 'Storage location' })).toBeInTheDocument();
    expect(screen.queryByText('SERVER WORDING')).not.toBeInTheDocument();
  });

  it('keeps the server\'s wording for a field the locale does not know yet', async () => {
    await show([served('a_field_from_next_year', 'Next year\'s field')]);

    expect(await screen.findByRole('button', { name: 'Next year\'s field' })).toBeInTheDocument();
  });

  it('offers where the spool lives, how hot it prints and when it was added — in both languages', () => {
    for (const locale of [en, uk]) {
      const named = (locale as { labelEditor: { placeholders: Record<string, { label: string; description: string }> } })
        .labelEditor.placeholders;
      for (const key of ['storage_location', 'nozzle_temp', 'added_date']) {
        expect(named[key].label).toBeTruthy();
        expect(named[key].description).toBeTruthy();
      }
    }
  });
});
