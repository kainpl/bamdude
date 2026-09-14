/**
 * A queued job shows itself from the bytes it owns — spec §4 / A09.
 *
 * Since m173 a job keeps an immutable local copy of what it prints, so it still
 * prints after its library file or archive is deleted. Its PICTURE did not
 * follow: both sites that build one need an original row's id, and the copy
 * dialog went further and *dropped* a row with neither — a job vanishing from
 * the list is worse than a job with no picture.
 *
 * The three things pinned here: the row is listed either way, its own snapshot's
 * render outranks the original's thumbnail (the original may have been re-sliced
 * since — A03), and a row with no recoverable picture draws its honest empty
 * state rather than a broken image.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { render as renderInApp } from '../utils';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../i18n';
import { CopyQueueModal } from '../../components/CopyQueueModal';
import { OrderQueue } from '../../components/projects/OrderQueue';
import { copyableItems, withCurrentPrint } from '../../lib/copyQueue';
import { api, type PrinterQueue, type PrinterStatus, type PrintQueueItem } from '../../api/client';

const SNAPSHOT_PICTURE = 'blob:the-snapshots-own-plate-render';

const item = (over: Partial<PrintQueueItem> = {}): PrintQueueItem =>
  ({
    id: 1,
    queue_id: 1,
    archive_id: null,
    library_file_id: 10,
    library_file_name: 'bracket.gcode.3mf',
    library_file_thumbnail: null,
    archive_name: null,
    archive_thumbnail: null,
    source_thumbnail: false,
    plate_id: null,
    project_id: null,
    project_line_id: null,
    project_name: null,
    position: 1,
    status: 'pending',
    ...over,
  }) as PrintQueueItem;

const independent = (over: Partial<PrintQueueItem> = {}): PrintQueueItem =>
  item({
    id: 42,
    archive_id: null,
    library_file_id: null,
    archive_name: null,
    library_file_name: 'lamp.gcode.3mf',
    source_storage: 'ready',
    source_thumbnail: true,
    ...over,
  });

const queue = (over: Partial<PrinterQueue> = {}): PrinterQueue =>
  ({
    id: 1,
    printer_id: 1,
    printer_name: 'P1S-A',
    printer_model: 'P1S',
    printer_location: null,
    status: 'idle',
    is_paused: false,
    pending_count: 0,
    completed_count: 0,
    failed_count: 0,
    cancelled_count: 0,
    skipped_count: 0,
    total_count: 0,
    ...over,
  }) as PrinterQueue;

const SOURCE = queue();

let createdFor: Blob[] = [];
let revoked: string[] = [];

beforeEach(() => {
  createdFor = [];
  revoked = [];
  vi.restoreAllMocks();
  window.URL.createObjectURL = vi.fn((blob: Blob) => {
    createdFor.push(blob);
    return SNAPSHOT_PICTURE;
  }) as never;
  window.URL.revokeObjectURL = vi.fn((url: string) => {
    revoked.push(url);
  }) as never;
});

afterEach(() => {
  vi.restoreAllMocks();
});

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
}

// --------------------------------------------------------------------------- //
// copyableItems: what is listed, and what can actually be copied
// --------------------------------------------------------------------------- //

describe('a job whose original rows are both gone', () => {
  it('is LISTED instead of silently dropped', () => {
    const rows = copyableItems([independent()]);

    expect(rows).toHaveLength(1);
    expect(rows[0].name).toBe('lamp.gcode.3mf');
  });

  it('can be copied from its own saved bytes', () => {
    expect(copyableItems([independent()])[0].file).toMatchObject({ id: 42, source: 'queue_snapshot' });
  });

  it('shows its own snapshot, so it is not a nameless pictureless row', () => {
    const [only] = copyableItems([independent()]);

    expect(only.pictureItemId).toBe(42);
    expect(only.thumbnailUrl).toBeNull();
  });

  it('says it has no picture when the snapshot renders none', () => {
    const [only] = copyableItems([independent({ source_thumbnail: false })]);

    expect(only.pictureItemId).toBeNull();
    expect(only.thumbnailUrl).toBeNull();
  });

  it('is not mistaken for the live print when deduplicating', () => {
    const rows = copyableItems([independent()]);

    const withLive = withCurrentPrint(rows, { current_archive_id: 42, subtask_name: 'Live' } as PrinterStatus);

    // The queue row's id is 42 and so is the live archive's — different spaces.
    expect(withLive.map((entry) => entry.name)).toEqual(['Live', 'lamp.gcode.3mf']);
  });
});

describe('a row whose original is still here', () => {
  it('prefers the job’s OWN render over the original’s thumbnail (A03)', () => {
    const [only] = copyableItems([
      item({ id: 7, library_file_id: 10, library_file_thumbnail: '/data/thumbs/10.png', source_thumbnail: true }),
    ]);

    expect(only.pictureItemId).toBe(7);
  });

  it('falls back to the original’s thumbnail when the job has no snapshot', () => {
    const [only] = copyableItems([item({ library_file_id: 10, library_file_thumbnail: '/data/thumbs/10.png' })]);

    expect(only.pictureItemId).toBeNull();
    expect(only.thumbnailUrl).toBe(api.getLibraryFileThumbnailUrl(10));
  });

  it('uses the job’s saved bytes even while its original is still present', () => {
    expect(copyableItems([item({ id: 7, source_storage: 'ready' })])[0].file).toMatchObject({ id: 7, source: 'queue_snapshot' });
  });
});

// --------------------------------------------------------------------------- //
// The copy dialog
// --------------------------------------------------------------------------- //

function renderModal(items: PrintQueueItem[]) {
  const onConfirm = vi.fn();
  render(
    <QueryClientProvider client={client()}>
      <I18nextProvider i18n={i18n}>
        <CopyQueueModal
          source={SOURCE}
          items={copyableItems(items)}
          onCancel={vi.fn()}
          onConfirm={onConfirm}
        />
      </I18nextProvider>
    </QueryClientProvider>,
  );
  return { onConfirm };
}

describe('the copy dialog', () => {
  beforeEach(() => {
    vi.spyOn(api, 'getQueues').mockResolvedValue([
      SOURCE,
      queue({ id: 2, printer_id: 2, printer_name: 'P1S-B', printer_model: 'P1S' }),
    ]);
    vi.spyOn(api, 'getPrinterStatus').mockResolvedValue({ connected: true, progress: 0 } as never);
    vi.spyOn(api, 'getQueueItemSourceThumbnail').mockResolvedValue(new Blob([new Uint8Array([1])]));
  });

  it('lists an independent job as a selectable saved source', async () => {
    renderModal([item({ id: 1 }), independent()]);

    expect(await screen.findByText('lamp.gcode.3mf')).toBeInTheDocument();
    expect(screen.queryByText(/original file is gone/i)).not.toBeInTheDocument();
  });

  it('ticks it, and «Select all» retains every saved source', async () => {
    renderModal([item({ id: 1 }), independent()]);
    const user = userEvent.setup();

    expect(await screen.findByText(/What to copy \(2\)/i)).toBeInTheDocument();
    await user.click(screen.getAllByText(/^Select all$/i)[0]);
    expect(screen.getByText(/What to copy \(2\)/i)).toBeInTheDocument();
  });

  it('hands a saved source to the run alongside legacy rows', async () => {
    const { onConfirm } = renderModal([item({ id: 1 }), independent()]);
    const user = userEvent.setup();

    await user.click(await screen.findByText('P1S-B'));
    await user.click(screen.getByRole('button', { name: /^Copy$/i }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    const [files] = onConfirm.mock.calls[0];
    expect(files).toHaveLength(2);
    expect(files[0]).toMatchObject({ id: 10, source: 'library' });
    expect(files[1]).toMatchObject({ id: 42, source: 'queue_snapshot' });
  });

  it('still needs a target printer before it can copy a saved source', async () => {
    renderModal([independent()]);

    expect(await screen.findByRole('button', { name: /^Copy$/i })).toBeDisabled();
  });

  it('draws the row’s own render, fetched with the session’s token', async () => {
    renderModal([independent()]);

    await waitFor(() => expect(api.getQueueItemSourceThumbnail).toHaveBeenCalledWith(42));
    await waitFor(() => expect(document.querySelector('img')).toHaveAttribute('src', SNAPSHOT_PICTURE));
  });

  it('draws no image at all when there is no picture to draw', async () => {
    renderModal([independent({ source_thumbnail: false })]);

    expect(await screen.findByText('lamp.gcode.3mf')).toBeInTheDocument();
    expect(api.getQueueItemSourceThumbnail).not.toHaveBeenCalled();
    expect(document.querySelector('img')).toBeNull();
  });
});

// --------------------------------------------------------------------------- //
// The order page's queue panel
// --------------------------------------------------------------------------- //

describe('the order page’s pending row', () => {
  const order = { id: 1, name: 'Ten flasks', status: 'active', lines: [{ id: 10, product_name: 'Flask' }] };

  beforeEach(() => {
    vi.spyOn(api, 'getOrder').mockResolvedValue(order as never);
    vi.spyOn(api, 'getSettings').mockResolvedValue({ time_format: 'system' } as never);
    vi.spyOn(api, 'getQueue').mockResolvedValue([] as never);
    vi.spyOn(api, 'getQueueItemSourceThumbnail').mockResolvedValue(new Blob([new Uint8Array([2])]));
  });

  function mount(rows: PrintQueueItem[]) {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 60_000 } },
    });
    queryClient.setQueryData(['queue', 'all', 'pending'], rows);
    queryClient.setQueryData(['queue', 'all', 'printing'], []);
    // The panel renders a <Link>, so it needs the app's router — the shared
    // wrapper provides it, and the inner client is what serves the seeded cache.
    return renderInApp(
      <QueryClientProvider client={queryClient}>
        <I18nextProvider i18n={i18n}>
          <OrderQueue orderId={1} />
        </I18nextProvider>
      </QueryClientProvider>,
    );
  }

  it('shows the job’s own render when its original rows are gone', async () => {
    mount([independent({ project_id: 1, project_line_id: 10, printer_id: 3, printer_name: 'P1S' })]);

    await waitFor(() => expect(api.getQueueItemSourceThumbnail).toHaveBeenCalledWith(42));
    await waitFor(() => expect(document.querySelector('img')).toHaveAttribute('src', SNAPSHOT_PICTURE));
  });

  it('prefers the job’s own render over the archive’s thumbnail', async () => {
    mount([
      independent({
        project_id: 1,
        project_line_id: 10,
        archive_id: 99,
        archive_thumbnail: '/data/thumbs/99.png',
        archive_name: 'Body',
      }),
    ]);

    await waitFor(() => expect(api.getQueueItemSourceThumbnail).toHaveBeenCalledWith(42));
    await waitFor(() => expect(document.querySelector('img')).toHaveAttribute('src', SNAPSHOT_PICTURE));
  });

  it('asks for nothing and draws the empty state when there is no picture', async () => {
    mount([
      independent({ project_id: 1, project_line_id: 10, source_thumbnail: false, library_file_name: 'lamp.3mf' }),
    ]);

    expect(await screen.findByText('lamp.3mf')).toBeInTheDocument();
    expect(api.getQueueItemSourceThumbnail).not.toHaveBeenCalled();
    expect(document.querySelector('img')).toBeNull();
  });

  it('revokes the object URL it made when the row goes away', async () => {
    const view = mount([independent({ project_id: 1, project_line_id: 10 })]);

    await waitFor(() => expect(createdFor).toHaveLength(1));
    view.unmount();
    expect(revoked).toEqual([SNAPSHOT_PICTURE]);
  });
});
