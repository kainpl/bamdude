/**
 * The auto-queue's own way in asks the same order question.
 *
 * The panel has no dialog of its own — "load from library" hands the chosen
 * files to `QueueSequencer`, which mounts `PrintModal` locked to auto mode. So
 * the Order field arrives here for free, and that is exactly why it is pinned
 * from the panel and not from the modal: the chain is what could break. A row
 * queued through the router still has to reach the order that needed it, and
 * the router picks the printer later, so the ids have to travel on the item.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import { render } from '../../utils';
import { AutoQueuePanel } from '../../../components/Queue/AutoQueuePanel';
import { ApiError, api } from '../../../api/client';
import type { LibraryFileListItem, LibraryGroupingMetadata, OrderCandidate } from '../../../api/client';
import type { AutoQueueItem } from '../../../api/client';

const CANDIDATE: OrderCandidate = {
  project_id: 4,
  project_name: 'Kickstarter batch',
  project_line_id: 9,
  product_id: 2,
  product_name: 'Desk Lamp',
  outstanding_prints: 5,
  priority: 2,
  deadline: null,
  created_at: '2026-09-01T10:14:02',
  line_material: null,
};

const FILE = {
  id: 5,
  folder_id: null,
  product_ids: [],
  is_external: false,
  filename: 'lamp.gcode.3mf',
  file_type: '3mf',
  file_tags: ['gcode', '3mf'],
  file_size: 1024,
  thumbnail_path: null,
  duplicate_count: 0,
  created_at: '2026-08-01T00:00:00Z',
  fs_modified_at: null,
  print_name: null,
  sliced_for_model: 'P1S',
} as unknown as LibraryFileListItem;

const GROUPING: LibraryGroupingMetadata = {
  file_id: 5,
  filename: 'lamp.gcode.3mf',
  sliced_for_model: 'P1S',
  nozzle_diameter: 0.4,
  bed_type: 'textured_plate',
  plates: [{ index: 1, filament_types: [], bed_type: 'textured_plate' }],
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'getAutoQueue').mockResolvedValue([]);
  vi.spyOn(api, 'getAutoQueueStats').mockResolvedValue({} as never);
  vi.spyOn(api, 'getLibraryFolders').mockResolvedValue([]);
  vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([FILE]);
  vi.spyOn(api, 'getLibraryGroupingMetadata').mockResolvedValue([GROUPING]);
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([])),
    http.get('/api/v1/library/files/:id', () =>
      HttpResponse.json({ id: 5, filename: 'lamp.gcode.3mf', file_tags: ['gcode', '3mf', 'sliced'] }),
    ),
    http.get('/api/v1/library/files/:id/plates', () =>
      HttpResponse.json({ is_multi_plate: false, plates: [] }),
    ),
    http.get('/api/v1/library/files/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
    http.get('/api/v1/library/files/:id/order-candidates', () => HttpResponse.json([CANDIDATE])),
  );
});

/** Panel → "load from library" → pick the file → the auto-mode dialog. */
async function openTheDialog(user: ReturnType<typeof userEvent.setup>) {
  render(<AutoQueuePanel />);

  await user.click(await screen.findByTitle('Load from library'));
  await user.click(await screen.findByText('lamp.gcode.3mf'));
  await user.click(screen.getByRole('button', { name: 'Add to queue' }));
}

describe('AutoQueuePanel — the order a routed print is filed under', () => {
  it('asks the order question and sends both ids to the router', async () => {
    const add = vi.spyOn(api, 'addToAutoQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    await openTheDialog(user);

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    await waitFor(() => expect(field.value).toBe('4:9'));

    await user.click(screen.getByRole('button', { name: /^add to queue$/i }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(expect.objectContaining({ project_id: 4, project_line_id: 9 })),
    );
  });

  it('lets the operator refuse the order the field proposed', async () => {
    const add = vi.spyOn(api, 'addToAutoQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    await openTheDialog(user);

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    await waitFor(() => expect(field.value).toBe('4:9'));
    await user.selectOptions(field, '');

    await user.click(screen.getByRole('button', { name: /^add to queue$/i }));

    await waitFor(() => expect(add).toHaveBeenCalled());
    expect(add.mock.calls[0][0]).toMatchObject({ project_id: undefined, project_line_id: null });
  });
});

function routerRow(overrides: Partial<AutoQueueItem>): AutoQueueItem {
  return {
    id: 1,
    archive_id: null,
    library_file_id: 5,
    project_id: 4,
    project_line_id: 9,
    target_model: 'P1S',
    target_location: null,
    target_location_id: null,
    required_filament_types: ['PLA'],
    filament_overrides: null,
    force_color_match: false,
    plate_id: 1,
    position: 1,
    scheduled_time: null,
    manual_start: false,
    auto_off_after: false,
    require_previous_success: false,
    bed_levelling: 'on',
    flow_cali: 'on',
    layer_inspect: false,
    timelapse: false,
    use_ams: true,
    mesh_mode_fast_check: true,
    execute_swap_macros: true,
    swap_macro_events: null,
    selected_macro_ids: null,
    status: 'pending',
    waiting_reason: null,
    assigned_to_item_id: null,
    assigned_at: null,
    cancelled_at: null,
    print_time_seconds: 3600,
    been_jumped: false,
    batch_id: null,
    created_at: '2026-09-10T10:00:00Z',
    created_by_id: null,
    library_file_name: 'lamp.gcode.3mf',
    ...overrides,
  };
}

describe('AutoQueuePanel — rebalancing across models', () => {
  it('offers Rebalance on a collapsed block and sends every copy of it', async () => {
    vi.spyOn(api, 'getAutoQueue').mockResolvedValue([
      routerRow({ id: 1, batch_id: 'b1', position: 1 }),
      routerRow({ id: 2, batch_id: 'b1', position: 2 }),
      routerRow({ id: 3, project_id: null, project_line_id: null, position: 3, library_file_name: 'loose.3mf' }),
    ]);
    const rebalance = vi
      .spyOn(api, 'rebalanceAutoQueueItems')
      .mockResolvedValue({ converted: 2, created: 4, cancelled: 0, moved_parts: 12, skipped: [] });
    const user = userEvent.setup();
    render(<AutoQueuePanel />);

    const buttons = await screen.findAllByTitle('Rebalance');
    expect(buttons).toHaveLength(1); // the block; the un-filed row offers none
    await user.click(buttons[0]);

    await waitFor(() => expect(rebalance).toHaveBeenCalledWith([1, 2]));
    expect(await screen.findByText('Moved 12 parts: 2 prints re-targeted, 4 added')).toBeInTheDocument();
  });

  it('says why a row stayed, and shows where a moved row came from', async () => {
    vi.spyOn(api, 'getAutoQueue').mockResolvedValue([
      routerRow({ id: 7, rebalanced_from_model: 'P1S', rebalanced_at: '2026-09-10T10:05:00Z', target_model: 'A1MINI' }),
    ]);
    vi.spyOn(api, 'rebalanceAutoQueueItems').mockResolvedValue({
      converted: 0, created: 0, cancelled: 0, moved_parts: 0, skipped: [{ item_id: 7, reason: 'no_faster_model' }],
    });
    const user = userEvent.setup();
    render(<AutoQueuePanel />);

    expect(await screen.findByTestId('auto-queue-rebalanced-7')).toHaveTextContent('← P1S');
    await user.click(await screen.findByTitle('Rebalance'));
    expect(await screen.findByText('No other model would finish it sooner')).toBeInTheDocument();
  });
});

describe('AutoQueuePanel — unavailable source', () => {
  it('shows the failed job and retries it without assigning or duplicating it', async () => {
    const failed = routerRow({ id: 71, status: 'failed', waiting_reason: 'Restore access to the file', batch_id: null });
    vi.mocked(api.getAutoQueue).mockResolvedValue([failed]);
    const retry = vi.spyOn(api, 'retryAutoQueue').mockImplementation(async () => {
      const pending = { ...failed, status: 'pending' as const, waiting_reason: null };
      vi.mocked(api.getAutoQueue).mockResolvedValue([pending]);
      return pending;
    });
    const assign = vi.spyOn(api, 'assignAutoQueueNow');
    const user = userEvent.setup();
    render(<AutoQueuePanel />);
    expect(await screen.findByText('File error')).toBeInTheDocument();
    expect(screen.getByText(/Restore access to the file/)).toBeInTheDocument();
    await user.click(screen.getByTitle('Retry'));
    await waitFor(() => expect(retry).toHaveBeenCalledWith(71));
    expect(assign).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByText('File error')).not.toBeInTheDocument());
  });

  it('keeps a failed copy visible beside a pending copy of the same batch', async () => {
    vi.mocked(api.getAutoQueue).mockResolvedValue([
      routerRow({ id: 1, batch_id: 'mixed', status: 'pending' }),
      routerRow({ id: 2, batch_id: 'mixed', status: 'failed', waiting_reason: 'File unavailable' }),
    ]);
    render(<AutoQueuePanel />);
    expect(await screen.findByText('File error')).toBeInTheDocument();
    expect(screen.getByTitle('Retry')).toBeInTheDocument();
    expect(screen.getByTitle('Assign now')).toBeInTheDocument();
    expect(screen.queryByText('×2')).not.toBeInTheDocument();
  });
});

describe('AutoQueuePanel - the copy of the file the router will print', () => {
  it('marks a row that keeps its own copy and explains one that is still being saved', async () => {
    vi.mocked(api.getAutoQueue).mockResolvedValue([
      routerRow({ id: 11, source_storage: 'ready' }),
      routerRow({ id: 12, source_storage: 'preparing', batch_id: null, library_file_name: 'other.3mf' }),
    ]);
    render(<AutoQueuePanel />);

    expect(
      await screen.findByTitle(
        'File saved for the queue — this job prints its own copy and no longer needs the original.',
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByTitle('Saving a copy of the file for the queue. The job waits here until the copy is finished.'),
    ).toBeInTheDocument();
  });

  it('says nothing was queued, and to try again, when the queue is already saving files', async () => {
    // The router tier is ONE request, so there is one answer to «was it added?»
    // — and a busy spool is the expected outcome of a burst, not a fault.
    vi.spyOn(api, 'addToAutoQueue').mockRejectedValue(
      new ApiError('server text for source_copy_busy', 503, 'source_copy_busy'),
    );
    const user = userEvent.setup();

    await openTheDialog(user);
    await user.click(screen.getByRole('button', { name: /^add to queue$/i }));

    expect(
      await screen.findByText(
        'Nothing was added to the queue. The queue is already saving other files — try again in a moment.',
      ),
    ).toBeInTheDocument();
  });
});
