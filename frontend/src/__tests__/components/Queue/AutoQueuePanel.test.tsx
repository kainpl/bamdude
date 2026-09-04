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
import { api } from '../../../api/client';
import type { LibraryFileListItem, LibraryGroupingMetadata, OrderCandidate } from '../../../api/client';

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
