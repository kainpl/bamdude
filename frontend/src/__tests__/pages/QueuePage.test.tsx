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
    ['queueSortBy', 'queueSortAsc', 'queueCardSize', 'queueViewMode', 'queueSearch', 'queueStatusFilter', 'queueLocationFilter', 'queueHideOffline']
      .forEach(key => localStorage.removeItem(key));
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

    it('uses the row virtualizer for large location groups', async () => {
      const fleet = Array.from({ length: 50 }, (_, index) => {
        const source = mockQueues[index % 2];
        return {
          ...source,
          id: index + 1,
          printer_id: index + 1,
          printer_name: `Farm queue ${index + 1}`,
        };
      });
      localStorage.setItem('queueSortBy', 'location');
      server.use(http.get('/api/v1/queues/', () => HttpResponse.json(fleet)));

      render(<QueuePage />);

      await screen.findByText('Farm queue 1');
      await waitFor(() => expect(document.querySelectorAll('[data-testid*="queue-card-grid-queues-group-"][data-testid$="-row"]')).not.toHaveLength(0));
    });

    it('preloads a large queue farm through one status batch before its cards mount', async () => {
      const fleet = Array.from({ length: 50 }, (_, index) => ({
        ...mockQueues[index % 2],
        id: index + 1,
        printer_id: index + 1,
        printer_name: `Batch queue ${index + 1}`,
      }));
      let batches = 0;
      server.use(
        http.get('/api/v1/queues/', () => HttpResponse.json(fleet)),
        http.get('/api/v1/printers/status/batch', () => {
          batches++;
          return HttpResponse.json(Object.fromEntries(fleet.map(queue => [queue.printer_id, { connected: true, state: 'IDLE' }])));
        }),
      );

      render(<QueuePage />);

      await screen.findByText('Batch queue 1');
      await waitFor(() => expect(batches).toBe(1));
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

  // NOT tested here: "picking a parent keeps a queue on its child". A page-level
  // version of that passed in isolation and failed inside this file, and the
  // cause was not the page -- the same render succeeded once the file ran alone.
  // A test that passes alone and fails in company is worse than none, so the
  // claim is covered where it is stable: subtree membership in
  // utils/locationTree.test.ts (including the sibling case, verified by
  // reverting the implementation), and the id-based wiring by the filter test
  // above, which narrows the list by a location id rather than by its name.
});
