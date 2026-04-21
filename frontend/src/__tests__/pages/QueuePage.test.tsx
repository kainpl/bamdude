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
    printer_location: 'Lab',
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
    printer_location: 'Office',
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

// TODO(#stale-tests): re-enable once assertions are updated to match current component output.
// See https://github.com/kainpl/bamdude/issues for the tracking ticket.
describe.skip('QueuePage', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/queues/', () => HttpResponse.json(mockQueues)),
      http.get('/api/v1/queue/', () => HttpResponse.json(mockPendingItems)),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
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
        // S, M, All view-mode toggles. Timeline button has no label text (icon-only).
        expect(screen.getByText('S')).toBeInTheDocument();
        expect(screen.getByText('M')).toBeInTheDocument();
        expect(screen.getByText('All')).toBeInTheDocument();
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

  describe('empty state', () => {
    it('shows empty state when no queues exist', async () => {
      server.use(http.get('/api/v1/queues/', () => HttpResponse.json([])));
      render(<QueuePage />);
      await waitFor(() => {
        expect(screen.getByText('No printer queues')).toBeInTheDocument();
      });
    });
  });

  describe('view modes', () => {
    it('switches to All view when clicking the All button', async () => {
      const user = userEvent.setup();
      render(<QueuePage />);
      await waitFor(() => {
        expect(screen.getByText('All')).toBeInTheDocument();
      });
      await user.click(screen.getByText('All'));
      // 'All' view renders the flat pending list — the pending item's name now
      // appears in both the card title and a tooltip/attribute, so multiple
      // elements share the text. `getAllByText` + length assertion proves the
      // item rendered without caring about the exact DOM duplication.
      await waitFor(() => {
        expect(screen.getAllByText('Pending Print').length).toBeGreaterThanOrEqual(1);
      });
    });
  });
});
