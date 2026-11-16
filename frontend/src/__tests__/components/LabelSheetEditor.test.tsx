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
import { screen, waitFor, within } from '@testing-library/react';
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
    vi.spyOn(api, 'checkLabelSheet').mockResolvedValue({ problems: [] });
  });

  const show = async (sheets: unknown[]) => {
    vi.spyOn(api, 'getLabelSheets').mockResolvedValue(sheets as never);
    render(<LabelSheetEditor />);
    // ⚠️ The heading is NOT the load evidence — it renders before the sheets
    // arrive. The panel's Name box is: it exists only once the query resolved,
    // `selectedId` picked the first row and the `draft` effect built the form
    // from it, which is three commits later. Reading a field synchronously
    // after the heading therefore races that chain, and loses it only under
    // load: this file went red both on the full parallel run and alone while
    // the backend suite saturated the CPU, then 7/7 on an idle machine.
    await screen.findByLabelText(/Name/);
  };

  it('still marks which ones came with BamDude', async () => {
    // Editable is not the same as indistinguishable — the badge says where a
    // row came from, it just no longer means "you may not touch this".
    await show([AVERY, MINE]);

    // The badge shares a line with the grid summary, so match the line.
    expect(screen.getByText(/built in/)).toBeInTheDocument();
  });

  it('lists the sheets with their grid', async () => {
    await show([AVERY, MINE]);

    expect(screen.getByText('Avery 5160')).toBeInTheDocument();
    expect(screen.getAllByText(/A4 · 3×7/).length).toBe(2);
  });

  it('lets you correct a seeded one', async () => {
    // ⚠️ This used to be refused, and the reversal is deliberate: Avery's
    // published numbers are what they are, and somebody who measures their own
    // stock and finds it 0.4mm off should be able to fix the row that is wrong
    // rather than leave it in the list beside a corrected copy.
    await show([AVERY]);

    expect((screen.getByLabelText(/Name/) as HTMLInputElement).disabled).toBe(false);
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

  it('asks before it deletes a sheet, naming it', async () => {
    // One click used to be enough, and a sheet is somebody's measured stock.
    const remove = vi.spyOn(api, 'deleteLabelSheet').mockResolvedValue(undefined as never);
    await show([MINE]);

    await userEvent.click(screen.getByRole('button', { name: /^Delete$/ }));

    expect(remove).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent(/My stock/);

    await userEvent.click(within(dialog).getByRole('button', { name: /^Delete$/ }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(2));
  });

  it('keeps the sheet when the question is cancelled', async () => {
    const remove = vi.spyOn(api, 'deleteLabelSheet').mockResolvedValue(undefined as never);
    await show([MINE]);

    await userEvent.click(screen.getByRole('button', { name: /^Delete$/ }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^Cancel$/ }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(remove).not.toHaveBeenCalled();
  });

  it('says what is wrong with the grid while it is typed, in the server\'s words', async () => {
    // The arithmetic is the server's: this editor asks, it never works it out.
    const check = vi
      .spyOn(api, 'checkLabelSheet')
      .mockImplementation(async (draft) => ({
        problems: draft.cols > 3 ? ['The grid is 260.0mm wide but A4 is 210.0mm.'] : [],
      }));
    await show([MINE]);

    const across = screen.getByLabelText(/Across/) as HTMLInputElement;
    await userEvent.clear(across);
    await userEvent.type(across, '4');

    expect(await screen.findByText(/260.0mm wide/)).toBeInTheDocument();
    expect(check).toHaveBeenLastCalledWith(expect.objectContaining({ cols: 4 }));
    // Save would be refused for the same reason, so it is not offered.
    expect(screen.getByRole('button', { name: /save/i })).toBeDisabled();
  });

  it('offers Save again once the grid fits', async () => {
    vi.spyOn(api, 'checkLabelSheet').mockImplementation(async (draft) => ({
      problems: draft.cols > 3 ? ['The grid is 260.0mm wide but A4 is 210.0mm.'] : [],
    }));
    await show([MINE]);

    const across = screen.getByLabelText(/Across/) as HTMLInputElement;
    await userEvent.clear(across);
    await userEvent.type(across, '4');
    await screen.findByText(/260.0mm wide/);
    await userEvent.clear(across);
    await userEvent.type(across, '3');

    await waitFor(() => expect(screen.queryByText(/260.0mm wide/)).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: /save/i })).not.toBeDisabled();
  });

  it('does not ask on every keystroke', async () => {
    const check = vi.spyOn(api, 'checkLabelSheet').mockResolvedValue({ problems: [] });
    await show([MINE]);
    await waitFor(() => expect(check).toHaveBeenCalledTimes(1));

    const left = screen.getByLabelText(/Left margin/) as HTMLInputElement;
    await userEvent.clear(left);
    await userEvent.type(left, '12.5');

    await waitFor(() => expect(check).toHaveBeenLastCalledWith(expect.objectContaining({ margin_left_mm: 12.5 })));
    // Five keystrokes, one question (plus the one on load).
    expect(check.mock.calls.length).toBeLessThanOrEqual(3);
  });

  it('reminds you to print a sheet at 100 %', async () => {
    await show([MINE]);

    expect(screen.getByText(/100\s?%/)).toBeInTheDocument();
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
