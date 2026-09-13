/**
 * Deleting the original, and what it does to the queue (spec §10, m173).
 *
 * ⚠️ The old sentence — "this will also remove N queued prints" — became a lie
 * the day a queued job started keeping its own copy of the file: those jobs stay
 * and still print, and only the ones still reading the original go with it. In a
 * bulk selection both halves are normally present, so the confirmation counts
 * them from the ROWS rather than claiming either for the whole set.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import { server } from '../mocks/server';
import { render } from '../utils';
import { QueueSpoolDeleteNote } from '../../components/QueueSpoolDeleteNote';
import { ApiError, api, type PrintQueueItem } from '../../api/client';
import { ConfirmModal } from '../../components/ConfirmModal';
import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';

const row = (over: Partial<PrintQueueItem>): PrintQueueItem =>
  ({
    id: 1,
    queue_id: 1,
    archive_id: null,
    library_file_id: null,
    status: 'pending',
    position: 1,
    ...over,
  }) as PrintQueueItem;

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('the queue note on a delete-the-original confirmation', () => {
  it('counts both halves of a mixed selection', async () => {
    const getQueue = vi.spyOn(api, 'getQueue').mockResolvedValue([
      row({ id: 1, library_file_id: 5, source_storage: 'ready' }),
      row({ id: 2, library_file_id: 5, source_storage: 'ready' }),
      row({ id: 3, library_file_id: 6, source_storage: 'legacy' }),
      // Still being copied, and a broken copy: neither is self-contained yet.
      row({ id: 4, library_file_id: 6, source_storage: 'preparing' }),
      row({ id: 5, library_file_id: 6, source_storage: 'broken' }),
      // Another file entirely — not part of this delete.
      row({ id: 6, library_file_id: 9, source_storage: 'legacy' }),
    ]);

    render(<QueueSpoolDeleteNote libraryFileIds={[5, 6]} />);

    expect(
      await screen.findByText(/Pending prints that keep their own saved copy: 2\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/Pending prints that still read these files: 3\./)).toBeInTheDocument();
    // ⚠️ Only PENDING work is asked for: a terminal row neither prints nor gets
    // cancelled, so counting it inflated one half and misdescribed the other.
    expect(getQueue).toHaveBeenCalledWith(undefined, 'pending');
  });

  it('says it could not check rather than falling silent', async () => {
    // Silence reads as «nothing else happens», which is the one thing an
    // unanswered query does not know — on a destructive confirmation.
    vi.spyOn(api, 'getQueue').mockRejectedValue(new ApiError('boom', 500));

    render(<QueueSpoolDeleteNote archiveIds={[12]} />);

    expect(
      await screen.findByText(/BamDude could not check which queued prints use these files\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/are cancelled with them/)).toBeInTheDocument();
  });

  it("admits the count is only this user's own rows when that is all they can read", async () => {
    // `GET /queue/` filters to the caller's own rows without `queue:read_all`,
    // so the count is a part of the picture by construction — a fact about the
    // permission, not a guess from a number.
    server.use(
      http.get('/api/v1/auth/me', () =>
        HttpResponse.json({
          id: 2,
          username: 'operator',
          role: 'user',
          is_active: true,
          is_admin: false,
          groups: [],
          permissions: ['queue:read_own', 'library:delete'],
          created_at: '2024-01-01T00:00:00Z',
        }),
      ),
    );
    vi.spyOn(api, 'getQueue').mockResolvedValue([row({ id: 1, library_file_id: 5, source_storage: 'legacy' })]);

    render(<QueueSpoolDeleteNote libraryFileIds={[5]} />);

    expect(
      await screen.findByText(/You can only see your own queued prints, so the counts above may not be all of them\./),
    ).toBeInTheDocument();
  });

  it('says nothing when no queued print names the source', async () => {
    vi.spyOn(api, 'getQueue').mockResolvedValue([row({ id: 1, archive_id: 77, source_storage: 'ready' })]);

    const { container } = render(<QueueSpoolDeleteNote archiveIds={[12]} />);

    // Not merely "empty at first": the note starts with a "checking…" line, so
    // the claim is that it ends up saying nothing once the rows are in.
    await waitFor(() => expect(container.textContent).toBe(''));
  });

  it('ignores the synthesised row of a print started outside the queue', async () => {
    // A virtual row is not a job anybody queued: there is nothing to cancel and
    // nothing to keep, so counting it either way would be an invented promise.
    vi.spyOn(api, 'getQueue').mockResolvedValue([
      row({ id: 1, archive_id: 12, is_virtual: true, source_storage: 'exempt' }),
    ]);

    const { container } = render(<QueueSpoolDeleteNote archiveIds={[12]} />);

    // Not merely "empty at first": the note starts with a "checking…" line, so
    // the claim is that it ends up saying nothing once the rows are in.
    await waitFor(() => expect(container.textContent).toBe(''));
  });
});


describe('where the note is said, and what the confirmations say without it', () => {
  it('reaches the screen through a ConfirmModal, beside its message', async () => {
    // The note is passed as `children`; nothing else proved it is rendered, and a
    // ConfirmModal refactor that dropped children would have left every test
    // green with the whole paragraph gone from a destructive dialog.
    vi.spyOn(api, 'getQueue').mockResolvedValue([row({ id: 1, archive_id: 12, source_storage: 'legacy' })]);

    render(
      <ConfirmModal
        title="Delete Archive"
        message="Queued prints backed by this archive: 1. The pending ones that still read it are cancelled with it."
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      >
        <QueueSpoolDeleteNote archiveIds={[12]} />
      </ConfirmModal>,
    );

    expect(await screen.findByText(/Pending prints that still read these files: 1\./)).toBeInTheDocument();
    // …and the consequence the archive line carries unconditionally is still
    // there, so a silent note never leaves a bare number behind.
    expect(screen.getByText(/The pending ones that still read it are cancelled with it\./)).toBeInTheDocument();
  });

  it('states the consequence in words where no count can exist', () => {
    // A folder delete trashes every file inside it and cancels their pending
    // prints. Nothing counts queued work under a folder, so the sentence carries
    // the consequence itself — including the half that surprises people: the
    // trash is reversible and the cancellation is not.
    for (const locale of [en, uk]) {
      const sentence = locale.fileManager.deleteFolderConfirm;
      expect(sentence.length).toBeGreaterThan(80);
      expect(sentence).not.toContain('{{');
    }
    expect(en.fileManager.deleteFolderConfirm).toContain('Pending prints in the queue');
    expect(en.fileManager.deleteFolderConfirm).toContain('restoring the files does not bring them back');
    expect(uk.fileManager.deleteFolderConfirm).toContain('Роботи в черзі');
    expect(uk.fileManager.deleteFolderConfirm).toContain('відновлення файлів їх не поверне');
  });
});
