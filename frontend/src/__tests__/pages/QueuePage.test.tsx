/**
 * Tests for the QueuePage component.
 *
 * The page was redesigned in 0.3.2 around per-printer queues with a
 * StatsBar + QueueCard layout (replacing the prior flat queue-items list
 * with status filters). Tests below cover the current UI: page title,
 * view-mode selector, sort dropdown, empty state, queue cards, stats bar.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import userEvent from '@testing-library/user-event';
import { screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { QueuePage } from '../../pages/QueuePage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockQueues = [
  {
    id: 1,
    printer_id: 1,
    printer_name: 'X1 Carbon',
    printer_model: 'X1C',
    printer_location: { id: 1, name: 'Lab', parent_id: null, path: 'Lab' },
    printer_tags: [{ id: 7, name: 'Phase 1', color: '#ff0000' }],
    status: 'idle',
    last_activity_at: null,
    current_item_id: null,
    pending_count: 2,
    completed_count: 5,
    failed_count: 0,
    cancelled_count: 0,
    skipped_count: 0,
    total_count: 7,
    created_at: '2026-04-14T00:00:00Z',
    updated_at: '2026-04-14T00:00:00Z',
  },
  {
    id: 2,
    printer_id: 2,
    printer_name: 'P1S',
    printer_model: 'P1S',
    printer_location: { id: 2, name: 'Office', parent_id: null, path: 'Office' },
    printer_tags: [],
    status: 'printing',
    last_activity_at: '2026-04-14T10:00:00Z',
    current_item_id: 42,
    pending_count: 1,
    completed_count: 3,
    failed_count: 0,
    cancelled_count: 0,
    skipped_count: 0,
    total_count: 4,
    created_at: '2026-04-14T00:00:00Z',
    updated_at: '2026-04-14T10:00:00Z',
  },
];

const mockPendingItems = [
  {
    id: 100,
    queue_id: 1,
    archive_id: 5,
    library_file_id: null,
    position: 1,
    status: 'pending',
    archive_name: 'Pending Print',
    printer_name: 'X1 Carbon',
    print_time_seconds: 3600,
    scheduled_time: null,
    auto_off_after: false,
    manual_start: false,
    plate_id: null,
    bed_levelling: true,
    flow_cali: false,
    layer_inspect: false,
    timelapse: false,
    use_ams: true,
    started_at: null,
    completed_at: null,
    error_message: null,
    created_at: '2026-04-14T00:00:00Z',
  },
];

describe('QueuePage', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/queues/', () => HttpResponse.json(mockQueues)),
      http.get('/api/v1/queue/', () => HttpResponse.json(mockPendingItems)),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      // The filter takes its options from the locations themselves now, so a
      // parent with nothing directly on it is still selectable.
      http.get('/api/v1/printer-locations', () =>
        HttpResponse.json({
          locations: [
            { id: 1, name: 'Lab', parent_id: null, path: 'Lab', depth: 1, printer_count: 1, sensor_count: 0, queued_count: 0 },
            { id: 2, name: 'Office', parent_id: null, path: 'Office', depth: 1, printer_count: 1, sensor_count: 0, queued_count: 0 },
          ],
        }),
      ),
      http.get('/api/v1/queue/forecast', () => HttpResponse.json({ free_at: '2026-09-06T12:00:00Z', free_seconds: 0, unknown_prints: 0 })),
    );
  });

  describe('rendering', () => {
    it('renders the page title', async () => {
      render(<QueuePage />);
      await waitFor(() => {
        expect(screen.getByText('Print Queue')).toBeInTheDocument();
      });
    });

    it('renders view-mode selector buttons', async () => {
      render(<QueuePage />);
      await waitFor(() => {
        // Cards / List / Timeline view-mode toggles. Old S/M/All labels were
        // dropped in 0.4.4 along with the rarely-used compact (S) mode.
        expect(screen.getByText('Cards')).toBeInTheDocument();
        expect(screen.getByText('List')).toBeInTheDocument();
        expect(screen.getByText('Timeline')).toBeInTheDocument();
      });
    });

    it('renders sort dropdown when not in All view', async () => {
      render(<QueuePage />);
      // Default view is 'expanded' (M), which keeps the sort dropdown visible.
      await waitFor(() => {
        const selects = document.querySelectorAll('select');
        expect(selects.length).toBeGreaterThan(0);
      });
    });
  });

  describe('queue cards', () => {
    it('renders one card per printer queue', async () => {
      render(<QueuePage />);
      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
        expect(screen.getByText('P1S')).toBeInTheDocument();
      });
    });

  });

  describe('location filter', () => {
    it('narrows to the picked place instead of emptying the page', async () => {
      // The filter compared the whole location row against the picked name, so
      // it was never equal and every queue disappeared. The stale string
      // fixture is why no test saw it.
      const user = userEvent.setup();
      render(<QueuePage />);
      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      // By id: the option values are location ids now, because a name stopped
      // being an identity when "Shelf 1" became possible under two workshops.
      const locationSelect = [...document.querySelectorAll('select')].find((select) =>
        [...select.options].some((option) => option.value === '1'),
      )!;
      await user.selectOptions(locationSelect, '1');

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
        expect(screen.queryByText('P1S')).not.toBeInTheDocument();
      });
    });
  });

  describe('empty state', () => {
    it('shows empty state when no queues exist', async () => {
      server.use(http.get('/api/v1/queues/', () => HttpResponse.json([])));
      render(<QueuePage />);
      await waitFor(() => {
        expect(screen.getByText('No printer queues')).toBeInTheDocument();
      });
    });
  });

  describe('sort', () => {
    it('offers both ETA orders and remembers the queue-aware one', async () => {
      // «ETA (job)» and «ETA (queue)» — the same pair the
      // printers page has, under the same keys, so the two pages read alike.
      const user = userEvent.setup();
      render(<QueuePage />);
      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });
      const sortSelect = [...document.querySelectorAll('select')].find((select) =>
        [...select.options].some((option) => option.value === 'freeAt'),
      )!;
      expect([...sortSelect.options].map((option) => option.value)).toEqual(
        expect.arrayContaining(['eta', 'freeAt']),
      );
      // By the option's own text: the toolbar renders once more inside its
      // overflow menu, hidden from the accessibility tree, so a role query is ambiguous.
      expect([...sortSelect.options].find((option) => option.value === 'eta')?.text).toBe('ETA (job)');
      expect([...sortSelect.options].find((option) => option.value === 'freeAt')?.text).toBe('ETA (queue)');
      await user.selectOptions(sortSelect, 'freeAt');
      expect(localStorage.getItem('queueSortBy')).toBe('freeAt');
      expect((sortSelect as HTMLSelectElement).value).toBe('freeAt');
    });

    it('groups the cards under their tags, «No tag» last, with the tag colour on the dot', async () => {
      // The printers page's tag view, on the queue page: the tagged X1 Carbon
      // under «Phase 1», the untagged P1S under «No tag», in that order. The
      // filters persist across tests in this file, so start from a clean slate.
      localStorage.clear();
      localStorage.setItem('queueSortBy', 'tag');
      const { container } = render(<QueuePage />);
      await waitFor(() => {
        expect(screen.getByText('Phase 1')).toBeInTheDocument();
        expect(screen.getByText('No tag')).toBeInTheDocument();
      });
      const headings = [...container.querySelectorAll('h2')].map((h) => h.textContent);
      expect(headings.indexOf('Phase 1(1)')).toBeLessThan(headings.indexOf('No tag(1)'));
      const dot = screen.getByText('Phase 1').closest('h2')!.querySelector('span');
      expect(dot).toHaveStyle({ backgroundColor: '#ff0000' });
    });
  });

  describe('card size', () => {
    it('remembers the picked size and lays the grid out for it', async () => {
      const user = userEvent.setup();
      const { container } = render(<QueuePage />);
      await waitFor(() => {
        expect(screen.getByText('S')).toBeInTheDocument();
      });
      // M by default — three columns at the widest, the table the page always had.
      expect(container.querySelector('.grid.gap-4')?.className).toContain('xl:grid-cols-3');
      await user.click(screen.getByText('S'));
      expect(localStorage.getItem('queueCardSize')).toBe('1');
      await waitFor(() => {
        expect(container.querySelector('.grid.gap-4')?.className).toContain('xl:grid-cols-4');
      });
      await user.click(screen.getByText('XL'));
      expect(localStorage.getItem('queueCardSize')).toBe('4');
      await waitFor(() => {
        expect(container.querySelector('.grid.gap-4')?.className).toContain('grid-cols-1');
        expect(container.querySelector('.grid.gap-4')?.className).not.toContain('xl:grid-cols');
      });
    });

    it('starts from the remembered size', async () => {
      localStorage.setItem('queueCardSize', '3');
      const { container } = render(<QueuePage />);
      await waitFor(() => {
        expect(container.querySelector('.grid.gap-4')?.className).toContain('lg:grid-cols-2');
      });
      expect(screen.getByText('L')).toHaveAttribute('aria-pressed', 'true');
    });
  });

  describe('view modes', () => {
    it('switches to List view when clicking the List button', async () => {
      const user = userEvent.setup();
      render(<QueuePage />);
      await waitFor(() => {
        expect(screen.getByText('List')).toBeInTheDocument();
      });
      await user.click(screen.getByText('List'));
      // 'List' view (formerly 'All') renders the flat pending list — the
      // pending item's name now appears in both the card title and a
      // tooltip/attribute, so multiple elements share the text.
      // `getAllByText` + length assertion proves the item rendered without
      // caring about the exact DOM duplication.
      await waitFor(() => {
        expect(screen.getAllByText('Pending Print').length).toBeGreaterThanOrEqual(1);
      });
    });
  });

  describe('auto-queue panel', () => {
    it('always renders even when there are no auto-queue items (drop target stays available)', async () => {
      // Dedicated /auto-queue/ endpoint returns empty — the panel should still
      // mount so the drag-drop overlay has something to bind to. Pre-fix the
      // panel returned `null` for empty state, hiding the drop zone whenever
      // nothing was queued.
      server.use(http.get('/api/v1/auto-queue/', () => HttpResponse.json([])));
      render(<QueuePage />);
      await waitFor(() => {
        // The empty-state hint text is keyed in i18n as autoQueue.emptyHint.
        expect(screen.getByText(/Drop a sliced file here/i)).toBeInTheDocument();
      });
    });
  });

  describe('stats bar', () => {
    it('shows the farm free-at estimate from the forecast endpoint', async () => {
      server.use(http.get('/api/v1/queue/forecast', () => HttpResponse.json({ free_at: '2026-09-06T13:30:00Z', free_seconds: 5400, unknown_prints: 0 })));
      render(<QueuePage />);
      expect(await screen.findByText('1h 30m')).toBeInTheDocument();
    });

    it('says how many rows carry no estimate, so «0m» is not a mystery', async () => {
      // A queued row or a print whose 3MF has not been attached has no
      // estimate, and the simulation counts those rather than defaulting them
      // to two hours — without the hint the tile just reads a small number.
      server.use(
        http.get('/api/v1/queue/forecast', () =>
          HttpResponse.json({ free_at: '2026-09-06T12:00:00Z', free_seconds: 0, unknown_prints: 2 }),
        ),
      );
      render(<QueuePage />);
      expect(await screen.findByText('2 prints without an estimate')).toBeInTheDocument();
    });
  });

  // Does this row own the bytes it prints? (spec §10, m173)
  //
  // The answer is one glyph with a tooltip, and that is the whole requirement:
  // no new card, no new row height, and silence where the question does not
  // arise. Pinned here rather than in a component test because the thing that
  // could break is the ROW — the mark shares the name line with the build-plate
  // icon, and a mark that needed its own block would change the geometry of
  // every queue on the farm.
  describe('the queue mark for a job that keeps its own file', () => {
    beforeEach(() => {
      // An earlier test in this file switches to the List view and PERSISTS it,
      // in localStorage and in the shared jsdom URL alike — and the flat list is
      // not the card's row. Ask for the cards explicitly rather than inherit
      // whatever ran before.
      localStorage.setItem('queueViewMode', 'expanded');
      window.history.replaceState({}, '', '/queue');
    });

    const spoolItem = (over: Record<string, unknown>) => ({
      ...mockPendingItems[0],
      ...over,
    });

    /**
     * One handler, answering each status query with only its own rows - and only
     * for the first printer's queue, or both cards would render the same job and
     * every lookup below would find it twice.
     */
    const byStatus = (rows: Record<string, unknown[]>) =>
      http.get('/api/v1/queue/', ({ request }) => {
        const params = new URL(request.url).searchParams;
        if (params.get('queue_id') !== '1') return HttpResponse.json([]);
        return HttpResponse.json(rows[params.get('status') ?? 'pending'] ?? []);
      });

    it('marks each state with its own explanation, and says nothing when there is none', async () => {
      server.use(
        byStatus({
          pending: [
            spoolItem({ id: 101, archive_name: 'Saved one', source_storage: 'ready', source_size_bytes: 2_097_152 }),
            spoolItem({ id: 102, archive_name: 'Copying one', source_storage: 'preparing' }),
            spoolItem({ id: 103, archive_name: 'Old one', source_storage: 'legacy' }),
            spoolItem({ id: 104, archive_name: 'Lost one', source_storage: 'broken' }),
            spoolItem({ id: 105, archive_name: 'External one', source_storage: 'exempt' }),
            spoolItem({ id: 106, archive_name: 'Silent one' }),
          ],
        }),
      );
      const user = userEvent.setup();
      render(<QueuePage />);

      // A collapsed card shows two rows; every state has to be on screen.
      await user.click(await screen.findByText(/Show \d+ more/));

      expect(
        await screen.findByTitle(
          'File saved for the queue (2.0 MB) — this job prints its own copy and no longer needs the original.',
        ),
      ).toBeInTheDocument();
      expect(
        screen.getByTitle('Saving a copy of the file for the queue. The job waits here until the copy is finished.'),
      ).toBeInTheDocument();
      expect(
        screen.getByTitle(
          'Queued before BamDude kept its own copies. It reads the original file, so keep that reachable until it prints.',
        ),
      ).toBeInTheDocument();
      expect(
        screen.getByTitle(
          'The saved copy is missing or damaged, so this job cannot print. Retry it to save the file again, or remove it from the queue.',
        ),
      ).toBeInTheDocument();

      // Four states are marked; `exempt` and an absent field are not — an
      // external print never had a supported source, so there is nothing to fix.
      expect(screen.getAllByRole('img', { name: /File saved|Saving the file|Uses the original|Saved copy lost/ }))
        .toHaveLength(4);
    });

    it('keeps the mark inside the row it belongs to, changing no geometry', async () => {
      server.use(
        byStatus({
          pending: [spoolItem({ id: 201, archive_name: 'Saved one', source_storage: 'ready' })],
        }),
      );
      render(<QueuePage />);

      const name = await screen.findByText('Saved one');
      const row = name.closest('div[class*="py-1.5"]')!;
      expect(row).not.toBeNull();
      // The mark lives in the row's own name line, beside the plate icon.
      expect(row.querySelector('[role="img"][aria-label="File saved"]')).not.toBeNull();
      // And the row is still the compact row it always was.
      expect(row.className).toContain('py-1.5');
      expect(row.className).toContain('px-2');
    });

    it('says a failed row is holding its file on purpose until it is removed', async () => {
      server.use(
        byStatus({
          pending: [],
          failed: [
            spoolItem({
              id: 301,
              status: 'failed',
              archive_name: 'Failed one',
              error_message: 'Printer offline',
              source_storage: 'ready',
            }),
          ],
        }),
      );
      const user = userEvent.setup();
      render(<QueuePage />);

      await user.click(await screen.findByText(/^Issues \(1\)$/));

      expect(
        await screen.findByTitle(/The saved file stays with this job until you remove it from the queue\./),
      ).toBeInTheDocument();
    });
  });

  // NOT tested here: "picking a parent keeps a queue on its child". A page-level
  // version of that passed in isolation and failed inside this file, and the
  // cause was not the page -- the same render succeeded once the file ran alone.
  // A test that passes alone and fails in company is worse than none, so the
  // claim is covered where it is stable: subtree membership in
  // utils/locationTree.test.ts (including the sibling case, verified by
  // reverting the implementation), and the id-based wiring by the filter test
  // above, which narrows the list by a location id rather than by its name.
});
