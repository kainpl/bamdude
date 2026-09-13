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

import { render } from '../utils';
import { QueueSpoolDeleteNote } from '../../components/QueueSpoolDeleteNote';
import { api, type PrintQueueItem } from '../../api/client';

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
    vi.spyOn(api, 'getQueue').mockResolvedValue([
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
      await screen.findByText(/Queued prints that keep their own saved copy: 2\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/Queued prints that still read these files: 3\./)).toBeInTheDocument();
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
