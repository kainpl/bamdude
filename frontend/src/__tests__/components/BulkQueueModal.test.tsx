/**
 * The count on the button is the feature.
 *
 * Everything else here is plumbing over a backend that already works. A bulk
 * action that quietly creates three times the work it appeared to is a ruined
 * evening on a farm, and the number before the click is the only defence — so
 * most of these tests are about that number, and one separate test checks that
 * what is POSTED matches what was promised. A dialog can display 15 and send
 * something else; those are different bugs and must fail independently.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { BulkQueueModal } from '../../components/BulkQueueModal';
import type { LibraryFileListItem } from '../../api/client';

const base = {
  file_path: '/library/x',
  file_size: 1024,
  folder_id: null,
  thumbnail_path: null,
  print_time_seconds: null,
  duplicate_count: 0,
  print_count: 0,
  notes_count: 0,
  tags: [],
  created_at: '2024-01-01T00:00:00Z',
} as unknown as LibraryFileListItem;

const single = { ...base, id: 1, filename: 'benchy.gcode.3mf', file_type: 'gcode', file_tags: ['gcode'], is_multi_plate: false };
const multi = { ...base, id: 2, filename: 'tray.gcode.3mf', file_type: 'gcode', file_tags: ['gcode'], is_multi_plate: true };
const raw = { ...base, id: 3, filename: 'bracket.stl', file_type: 'stl', file_tags: ['stl'], is_multi_plate: false };

const PRINTERS = [
  { id: 10, name: 'P1', model: 'X1C', is_active: true },
  { id: 11, name: 'P2', model: 'X1C', is_active: true },
];

let plateRequests: number[] = [];
let posted: unknown = null;

function mockApi() {
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json(PRINTERS)),
    http.get('/api/v1/library/files/:id/plates', ({ params }) => {
      plateRequests.push(Number(params.id));
      return HttpResponse.json({
        file_id: Number(params.id),
        filename: 'tray.gcode.3mf',
        plates: [1, 2, 3].map((i) => ({
          index: i,
          name: null,
          objects: [],
          has_thumbnail: false,
          thumbnail_url: null,
          print_time_seconds: null,
          filament_used_grams: null,
          filaments: [],
        })),
      });
    }),
    http.post('/api/v1/library/files/queue', async ({ request }) => {
      posted = await request.json();
      return HttpResponse.json({ added: [{ file_id: 1, filename: 'x', plate_id: null, printer_id: 10, queue_item_id: 1 }], errors: [] });
    }),
  );
}

const open = (files: LibraryFileListItem[]) =>
  render(<BulkQueueModal files={files} onClose={() => {}} />);

describe('BulkQueueModal', () => {
  beforeEach(() => {
    plateRequests = [];
    posted = null;
    mockApi();
  });

  it('counts one print per single-plate file', async () => {
    open([single]);
    expect(await screen.findByRole('button', { name: /Add 1 print$/ })).toBeInTheDocument();
  });

  it('counts every plate of a multi-plate file', async () => {
    open([single, multi]);
    // 1 + 3 plates, all ticked by default — "queue this file" means its whole
    // contents, not its first plate.
    expect(await screen.findByRole('button', { name: /Add 4 prints/ })).toBeInTheDocument();
  });

  it('drops a print from the count when a plate is unticked', async () => {
    open([multi]);
    await screen.findByRole('button', { name: /Add 3 prints/ });

    await userEvent.click(screen.getByRole('checkbox', { name: '2' }));

    expect(await screen.findByRole('button', { name: /Add 2 prints/ })).toBeInTheDocument();
  });

  it('multiplies by the number of printers on "each"', async () => {
    open([single, multi]);
    await screen.findByRole('button', { name: /Add 4 prints/ });

    await userEvent.click(screen.getByRole('button', { name: 'P1' }));
    await userEvent.click(screen.getByRole('button', { name: 'P2' }));

    expect(await screen.findByRole('button', { name: /Add 8 prints/ })).toBeInTheDocument();
  });

  it('does not multiply on "spread"', async () => {
    // The distinction the two modes exist for: spread produces N prints across
    // the machines, not N per machine.
    open([single, multi]);
    await userEvent.click(await screen.findByRole('button', { name: 'P1' }));
    await userEvent.click(screen.getByRole('button', { name: 'P2' }));
    await userEvent.click(screen.getByRole('radio', { name: /spread/i }));

    expect(await screen.findByRole('button', { name: /Add 4 prints/ })).toBeInTheDocument();
  });

  it('excludes an unsliced file from the count and says why', async () => {
    open([single, raw]);

    expect(await screen.findByRole('button', { name: /Add 1 print$/ })).toBeInTheDocument();
    const row = (await screen.findByText('bracket.stl')).closest('[data-bulk-row]') as HTMLElement;
    expect(within(row).getByText(/not sliced/i)).toBeInTheDocument();
  });

  it('asks for no plates when nothing in the selection is multi-plate', async () => {
    // The whole reason is_multi_plate rides on the listing.
    open([single, raw]);
    await screen.findByRole('button', { name: /Add 1 print$/ });

    expect(plateRequests).toEqual([]);
  });

  it('hides the printer picker and the mode toggle for the auto-queue', async () => {
    open([single]);
    await screen.findByRole('button', { name: 'P1' });

    await userEvent.click(screen.getByRole('radio', { name: /auto/i }));

    expect(screen.queryByRole('button', { name: 'P1' })).not.toBeInTheDocument();
    expect(screen.queryByRole('radio', { name: /spread/i })).not.toBeInTheDocument();
  });

  it('posts the ticked plates, the chosen printers and the mode', async () => {
    open([single, multi]);
    // Waits on the plates arriving, not on the label — see the click below.
    await screen.findByRole('checkbox', { name: '2' });

    await userEvent.click(screen.getByRole('checkbox', { name: '2' }));
    await userEvent.click(screen.getByRole('button', { name: 'P1' }));
    // Found by a stable hook, not by the count — otherwise this test could not
    // fail independently of the label, and "shows 15 but posts something else"
    // is exactly the bug worth catching separately.
    await userEvent.click(document.querySelector('[data-submit]') as HTMLElement);

    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toEqual({
      items: [{ file_id: 1 }, { file_id: 2, plate_ids: [1, 3] }],
      destination: { kind: 'printers', printer_ids: [10], mode: 'each' },
    });
  });
});
