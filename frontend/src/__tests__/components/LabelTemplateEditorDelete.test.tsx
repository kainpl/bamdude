/**
 * Deleting a design asks first.
 *
 * The bin beside Save used to delete on the click — one stray press took a
 * design somebody spent an evening drawing, with no way back. For a design
 * that came with BamDude the question also says what deleting it means: the
 * row leaves the list and the print API goes back to the shipped definition.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { LabelTemplateEditor } from '../../components/labels/LabelTemplateEditor';
import { api } from '../../api/client';

// Konva needs a real canvas; the drawing surface is not what is under test.
vi.mock('../../components/labels/LabelCanvas', () => ({ LabelCanvas: () => null }));

const MINE = {
  id: 4,
  name: 'Shelf tag',
  description: '',
  width_mm: 40,
  height_mm: 20,
  shape: 'rect',
  target: 'driver',
  elements: [],
  builtin_key: null,
  is_builtin: false,
};

const SEEDED = { ...MINE, id: 5, name: 'Box label 40 × 30', builtin_key: 'box_40x30', is_builtin: true };

describe('deleting a label design', () => {
  let remove: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getLabelTemplates').mockResolvedValue([MINE, SEEDED] as never);
    vi.spyOn(api, 'getLabelPlaceholders').mockResolvedValue([] as never);
    vi.spyOn(api, 'getUiPreferences').mockResolvedValue({} as never);
    vi.spyOn(api, 'previewLabelTemplate').mockResolvedValue({ blob: new Blob(), warnings: [] } as never);
    remove = vi.spyOn(api, 'deleteLabelTemplate').mockResolvedValue(undefined as never);
    Object.defineProperty(window.URL, 'createObjectURL', { value: vi.fn(() => 'blob:mock'), configurable: true });
    Object.defineProperty(window.URL, 'revokeObjectURL', { value: vi.fn(), configurable: true });
  });

  const openDesign = async (name: string) => {
    render(<LabelTemplateEditor />);
    await userEvent.click(await screen.findByRole('button', { name: new RegExp(name) }));
    return screen.findByRole('button', { name: /^Delete$/ });
  };

  it('asks before it deletes, naming the design', async () => {
    await userEvent.click(await openDesign('Shelf tag'));

    expect(remove).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent(/Shelf tag/);

    await userEvent.click(within(dialog).getByRole('button', { name: /^Delete$/ }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(4));
  });

  it('keeps the design when the question is cancelled', async () => {
    await userEvent.click(await openDesign('Shelf tag'));

    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^Cancel$/ }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(remove).not.toHaveBeenCalled();
  });

  it('says what deleting a design that came with BamDude means', async () => {
    await userEvent.click(await openDesign('Box label'));

    expect(await screen.findByRole('dialog')).toHaveTextContent(/came with BamDude/i);
  });
});
