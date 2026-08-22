/**
 * Drawing your own page of stickers.
 *
 * The sheet table has had two seeded rows and no way to add a third since it
 * was created — "choosing between two is fine, drawing a third is not". These
 * cover the rules that make the difference between a geometry you can trust and
 * one you discover on adhesive stock: a seeded sheet is read-only, and a grid
 * that runs off its paper says so where you can see it.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { LabelSheetEditor } from '../../components/labels/LabelSheetEditor';
import { api } from '../../api/client';

const AVERY = {
  id: 1,
  name: 'Avery 5160',
  builtin_key: 'avery_5160',
  page_size: 'A4' as const,
  cell_width_mm: 63.5,
  cell_height_mm: 38.1,
  cols: 3,
  rows: 7,
  margin_top_mm: 15,
  margin_left_mm: 7,
  gap_x_mm: 2.5,
  gap_y_mm: 0,
  is_builtin: true,
  overflow: [],
};

const MINE = { ...AVERY, id: 2, name: 'My stock', builtin_key: null, is_builtin: false };

describe('LabelSheetEditor', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getLabelTemplates').mockResolvedValue([] as never);
  });

  const show = async (sheets: unknown[]) => {
    vi.spyOn(api, 'getLabelSheets').mockResolvedValue(sheets as never);
    render(<LabelSheetEditor />);
    await waitFor(() => expect(screen.getByText('Sheets of stickers')).toBeInTheDocument());
  };

  it('lists the sheets with their grid', async () => {
    await show([AVERY, MINE]);

    expect(screen.getByText('Avery 5160')).toBeInTheDocument();
    expect(screen.getAllByText(/A4 · 3×7/).length).toBe(2);
  });

  it('refuses to edit a built-in one', async () => {
    // ⚠️ Same rule as a built-in design: an automation printing onto this paper
    // for a year must not find the grid moved under it.
    await show([AVERY]);

    expect((screen.getByLabelText(/Name/) as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText(/cannot be edited/)).toBeInTheDocument();
  });

  it('lets you edit your own', async () => {
    await show([MINE]);

    expect((screen.getByLabelText(/Name/) as HTMLInputElement).disabled).toBe(false);
  });

  it('says which sheets do not fit their paper', async () => {
    // ⚠️ A geometry can stop fitting without anyone editing it — change the
    // paper under it and the same grid runs off the page. The list is where
    // that has to show, not the printer.
    await show([{ ...MINE, overflow: ['The grid is 260.0mm wide but A4 is 210.0mm.'] }]);

    expect(screen.getByText('Does not fit the paper')).toBeInTheDocument();
    expect(screen.getByText(/260.0mm wide/)).toBeInTheDocument();
  });

  it('saves the numbers you typed', async () => {
    const update = vi.spyOn(api, 'updateLabelSheet').mockResolvedValue(MINE as never);
    await show([MINE]);

    const across = screen.getByLabelText(/Across/) as HTMLInputElement;
    await userEvent.clear(across);
    await userEvent.type(across, '4');
    await userEvent.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => expect(update).toHaveBeenCalledWith(2, expect.objectContaining({ cols: 4 })));
  });

  it('previews the page only when asked', async () => {
    // ⚠️ Not on every keystroke: a page render is a whole PDF, unlike the
    // design editor's single label.
    const preview = vi
      .spyOn(api, 'previewLabelSheet')
      .mockResolvedValue({ blob: new Blob(['%PDF']), warnings: [] } as never);
    vi.spyOn(api, 'getLabelTemplates').mockResolvedValue([{ id: 9, name: 'Box 40×30' }] as never);
    await show([MINE]);

    const across = screen.getByLabelText(/Across/) as HTMLInputElement;
    await userEvent.clear(across);
    await userEvent.type(across, '2');
    expect(preview).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: /preview the page/i }));

    await waitFor(() => expect(preview).toHaveBeenCalledWith({ sheet: expect.objectContaining({ cols: 2 }), template_id: 9 }));
  });
});
