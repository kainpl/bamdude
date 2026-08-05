/**
 * The tag dialog, reworked.
 *
 * Its worst fault was never the click count: the whole row was a click target
 * that applied a filter AND closed the window, so missing the 16-pixel trash
 * icon punished the cheapest possible mistake with the most expensive outcome.
 * Renaming opened a nested modal for one text field; deleting stacked a third
 * layer on that; the file count lived in a column and was not on screen at the
 * moment of the decision.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { LibraryTagsModal } from '../../components/LibraryTagsModal';

const stamps = { created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z' };

const TAGS = [
  { id: 1, name: '3MF', file_count: 12, is_system: true, code: '3mf', ...stamps },
  { id: 2, name: 'kid-safe', file_count: 12, is_system: false, code: null, ...stamps },
  { id: 3, name: 'petg-only', file_count: 0, is_system: false, code: null, ...stamps },
];

let patched: { id: number; name: string }[] = [];
let deleted: number[] = [];

describe('LibraryTagsModal', () => {
  beforeEach(() => {
    patched = [];
    deleted = [];
    server.use(
      http.get('/api/v1/library/tags', () => HttpResponse.json(TAGS)),
      http.patch('/api/v1/library/tags/:id', async ({ params, request }) => {
        const body = (await request.json()) as { name: string };
        patched.push({ id: Number(params.id), name: body.name });
        return HttpResponse.json({ ...TAGS[1], name: body.name });
      }),
      http.delete('/api/v1/library/tags/:id', ({ params }) => {
        deleted.push(Number(params.id));
        return new HttpResponse(null, { status: 204 });
      }),
    );
  });

  const rowFor = async (name: string) => (await screen.findByText(name)).closest('[data-tag-row]') as HTMLElement;

  it('shows a system tag without any controls', async () => {
    render(<LibraryTagsModal open onClose={() => {}} />);

    const row = await rowFor('3MF');
    expect(within(row).queryByRole('checkbox')).not.toBeInTheDocument();
    expect(within(row).queryByRole('button')).not.toBeInTheDocument();
    // It is still shown, with its count — "what exists and how much is on it"
    // is worth answering for both kinds.
    expect(within(row).getByText('12')).toBeInTheDocument();
  });

  it('does nothing at all when a row is clicked', async () => {
    // THE fault. The old dialog treated the row as a filter target and closed
    // itself, so a near-miss on the trash icon silently narrowed the library
    // and took the window away.
    const onClose = vi.fn();
    render(<LibraryTagsModal open onClose={onClose} />);

    await userEvent.click(await rowFor('kid-safe'));
    await userEvent.click(await rowFor('3MF'));

    expect(onClose).not.toHaveBeenCalled();
  });

  it('renames in the row on Enter', async () => {
    render(<LibraryTagsModal open onClose={() => {}} />);

    await userEvent.click(within(await rowFor('kid-safe')).getByText('kid-safe'));
    const input = await screen.findByDisplayValue('kid-safe');
    await userEvent.clear(input);
    await userEvent.type(input, 'kid-friendly{Enter}');

    await waitFor(() => expect(patched).toEqual([{ id: 2, name: 'kid-friendly' }]));
  });

  it('abandons a rename on Escape', async () => {
    render(<LibraryTagsModal open onClose={() => {}} />);

    await userEvent.click(within(await rowFor('kid-safe')).getByText('kid-safe'));
    const input = await screen.findByDisplayValue('kid-safe');
    await userEvent.clear(input);
    await userEvent.type(input, 'nope{Escape}');

    expect(patched).toEqual([]);
    expect(await screen.findByText('kid-safe')).toBeInTheDocument();
  });

  it('says how many files a delete will affect, before it happens', async () => {
    // The count used to live in a column, which meant it was on screen but not
    // in the sentence you were agreeing to.
    render(<LibraryTagsModal open onClose={() => {}} />);

    await userEvent.click(within(await rowFor('kid-safe')).getByRole('button', { name: /delete/i }));

    expect(await screen.findByText(/12 files/)).toBeInTheDocument();
    expect(deleted).toEqual([]);

    await userEvent.click(screen.getByRole('button', { name: /^Delete$/ }));
    await waitFor(() => expect(deleted).toEqual([2]));
  });

  it('deletes several at once', async () => {
    render(<LibraryTagsModal open onClose={() => {}} />);

    await userEvent.click(within(await rowFor('kid-safe')).getByRole('checkbox'));
    await userEvent.click(within(await rowFor('petg-only')).getByRole('checkbox'));
    await userEvent.click(await screen.findByRole('button', { name: /delete 2 tags/i }));
    await userEvent.click(await screen.findByRole('button', { name: /^Delete$/ }));

    await waitFor(() => expect(deleted.sort()).toEqual([2, 3]));
  });

  it('searches across both sections', async () => {
    render(<LibraryTagsModal open onClose={() => {}} />);
    await screen.findByText('kid-safe');

    await userEvent.type(screen.getByPlaceholderText(/filter tags/i), '3mf');

    expect(await screen.findByText('3MF')).toBeInTheDocument();
    expect(screen.queryByText('kid-safe')).not.toBeInTheDocument();
  });

  it('has no nested dialog anywhere', async () => {
    // Rename used to open a modal on a modal; delete stacked a third. Exactly
    // one dialog is on screen at any point in this component's life.
    render(<LibraryTagsModal open onClose={() => {}} />);

    await userEvent.click(within(await rowFor('kid-safe')).getByText('kid-safe'));
    expect(screen.getAllByRole('dialog')).toHaveLength(1);

    await userEvent.keyboard('{Escape}');
    await userEvent.click(within(await rowFor('kid-safe')).getByRole('button', { name: /delete/i }));
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
  });
});
