/**
 * Tests for LibraryFileNotesPopover (gh#3).
 *
 * Covers: empty-state create form, single-note render, pagination between
 * multiple notes, character counter, author-only edit/delete gating via
 * can_edit flag, and save round-trip.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { useRef } from 'react';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { LibraryFileNotesPopover } from '../../components/LibraryFileNotesPopover';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

// Harness component: renders the popover with a stable ref + onClose spy.
function Harness({ fileId, onClose = vi.fn(), onCountChange = vi.fn() }: {
  fileId: number;
  onClose?: () => void;
  onCountChange?: (n: number) => void;
}) {
  const anchorRef = useRef<HTMLButtonElement>(null);
  return (
    <div>
      <button ref={anchorRef} data-testid="anchor" type="button">anchor</button>
      <LibraryFileNotesPopover
        fileId={fileId}
        open={true}
        anchorRef={anchorRef}
        onClose={onClose}
        onCountChange={onCountChange}
      />
    </div>
  );
}

describe('LibraryFileNotesPopover', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows create form when the file has no notes', async () => {
    server.use(http.get('/api/v1/library/files/1/notes', () => HttpResponse.json([])));
    render(<Harness fileId={1} />);
    await waitFor(() => {
      expect(screen.getByPlaceholderText('Type your note…')).toBeInTheDocument();
    });
    // char counter visible
    expect(screen.getByTestId('notes-char-counter')).toHaveTextContent('1000 chars left');
  });

  it('renders a single note with its body and meta', async () => {
    const note = {
      id: 10,
      library_file_id: 1,
      user_id: 1,
      user_username: 'alice',
      body: 'This is note one.',
      created_at: '2026-04-14T10:00:00Z',
      updated_at: '2026-04-14T10:00:00Z',
      can_edit: true,
    };
    server.use(http.get('/api/v1/library/files/1/notes', () => HttpResponse.json([note])));
    render(<Harness fileId={1} />);

    await waitFor(() => {
      expect(screen.getByText('This is note one.')).toBeInTheDocument();
    });
    // meta line contains author
    expect(screen.getByText(/alice/)).toBeInTheDocument();
  });

  it('paginates through multiple notes', async () => {
    const notes = [
      { id: 3, library_file_id: 1, user_id: 1, user_username: 'alice', body: 'newest', created_at: '2026-04-14T12:00:00Z', updated_at: '2026-04-14T12:00:00Z', can_edit: true },
      { id: 2, library_file_id: 1, user_id: 1, user_username: 'alice', body: 'middle', created_at: '2026-04-14T11:00:00Z', updated_at: '2026-04-14T11:00:00Z', can_edit: true },
      { id: 1, library_file_id: 1, user_id: 1, user_username: 'alice', body: 'oldest', created_at: '2026-04-14T10:00:00Z', updated_at: '2026-04-14T10:00:00Z', can_edit: true },
    ];
    server.use(http.get('/api/v1/library/files/1/notes', () => HttpResponse.json(notes)));
    const user = userEvent.setup();
    render(<Harness fileId={1} />);

    await waitFor(() => {
      expect(screen.getByText('newest')).toBeInTheDocument();
      expect(screen.getByText('1 / 3')).toBeInTheDocument();
    });

    await user.click(screen.getByLabelText('Next note'));
    await waitFor(() => expect(screen.getByText('middle')).toBeInTheDocument());
    expect(screen.getByText('2 / 3')).toBeInTheDocument();

    await user.click(screen.getByLabelText('Next note'));
    await waitFor(() => expect(screen.getByText('oldest')).toBeInTheDocument());
    expect(screen.getByText('3 / 3')).toBeInTheDocument();

    // Next is now disabled
    const nextBtn = screen.getByLabelText('Next note') as HTMLButtonElement;
    expect(nextBtn.disabled).toBe(true);

    await user.click(screen.getByLabelText('Previous note'));
    await waitFor(() => expect(screen.getByText('middle')).toBeInTheDocument());
  });

  it('hides edit and delete buttons for notes the user cannot edit', async () => {
    const note = {
      id: 10,
      library_file_id: 1,
      user_id: 42,
      user_username: 'bob',
      body: 'someone else wrote this',
      created_at: '2026-04-14T10:00:00Z',
      updated_at: '2026-04-14T10:00:00Z',
      can_edit: false,
    };
    server.use(http.get('/api/v1/library/files/1/notes', () => HttpResponse.json([note])));
    render(<Harness fileId={1} />);

    await waitFor(() => {
      expect(screen.getByText('someone else wrote this')).toBeInTheDocument();
    });

    expect(screen.queryByLabelText('Edit note')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Delete note')).not.toBeInTheDocument();
  });

  it('shows edit and delete buttons for the author', async () => {
    const note = {
      id: 10,
      library_file_id: 1,
      user_id: 1,
      user_username: 'alice',
      body: 'mine',
      created_at: '2026-04-14T10:00:00Z',
      updated_at: '2026-04-14T10:00:00Z',
      can_edit: true,
    };
    server.use(http.get('/api/v1/library/files/1/notes', () => HttpResponse.json([note])));
    render(<Harness fileId={1} />);

    await waitFor(() => {
      expect(screen.getByText('mine')).toBeInTheDocument();
    });
    expect(screen.getByLabelText('Edit note')).toBeInTheDocument();
    expect(screen.getByLabelText('Delete note')).toBeInTheDocument();
  });

  it('updates char counter as the user types', async () => {
    server.use(http.get('/api/v1/library/files/1/notes', () => HttpResponse.json([])));
    render(<Harness fileId={1} />);

    const textarea = await waitFor(() => screen.getByPlaceholderText('Type your note…'));
    fireEvent.change(textarea, { target: { value: 'hello' } });
    await waitFor(() => {
      expect(screen.getByTestId('notes-char-counter')).toHaveTextContent('995 chars left');
    });
  });

  it('save button is disabled until the draft has non-whitespace content', async () => {
    server.use(http.get('/api/v1/library/files/1/notes', () => HttpResponse.json([])));
    render(<Harness fileId={1} />);

    const saveBtn = await waitFor(() => screen.getByLabelText('Save') as HTMLButtonElement);
    expect(saveBtn.disabled).toBe(true);

    const textarea = screen.getByPlaceholderText('Type your note…');
    fireEvent.change(textarea, { target: { value: '   ' } });
    expect(saveBtn.disabled).toBe(true);  // whitespace only

    fireEvent.change(textarea, { target: { value: 'real' } });
    await waitFor(() => expect(saveBtn.disabled).toBe(false));
  });

  it('creates a note on save and notifies the parent of the new count', async () => {
    server.use(
      http.get('/api/v1/library/files/1/notes', () => HttpResponse.json([])),
      http.post('/api/v1/library/files/1/notes', async ({ request }) => {
        const body = await request.json() as { body: string };
        return HttpResponse.json({
          id: 100,
          library_file_id: 1,
          user_id: 1,
          user_username: 'alice',
          body: body.body,
          created_at: '2026-04-14T10:00:00Z',
          updated_at: '2026-04-14T10:00:00Z',
          can_edit: true,
        });
      }),
    );
    const onCountChange = vi.fn();
    render(<Harness fileId={1} onCountChange={onCountChange} />);

    const textarea = await waitFor(() => screen.getByPlaceholderText('Type your note…'));
    fireEvent.change(textarea, { target: { value: 'brand new note' } });

    const user = userEvent.setup();
    await user.click(screen.getByLabelText('Save'));

    await waitFor(() => {
      expect(onCountChange).toHaveBeenCalledWith(1);
    });
    // After save we drop into view mode; the new note is visible.
    await waitFor(() => expect(screen.getByText('brand new note')).toBeInTheDocument());
  });
});
