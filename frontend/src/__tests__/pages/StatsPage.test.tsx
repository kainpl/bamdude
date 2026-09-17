/**
 * Tests for the StatsPage component.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import { render } from '../utils';
import { StatsPage } from '../../pages/StatsPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

// Complete mock stats matching ArchiveStats interface
const mockStats = {
  total_prints: 150,
  successful_prints: 140,
  failed_prints: 10,
  total_print_time_hours: 500.5,
  total_filament_grams: 5500,
  total_cost: 125.50,
  prints_by_filament_type: {
    'PLA': 80,
    'PETG': 50,
    'ABS': 20,
  },
  prints_by_printer: {
    '1': 100,
    '2': 50,
  },
  average_time_accuracy: 98.5,
  time_accuracy_by_printer: {
    '1': 99.0,
    '2': 97.0,
  },
  total_energy_kwh: 45.5,
  total_energy_cost: 12.50,
};

const mockPrinters = [
  { id: 1, name: 'X1 Carbon', model: 'X1C', enabled: true },
  { id: 2, name: 'P1S', model: 'P1S', enabled: true },
];

/**
 * The Stats page reads `GET /statistics/aggregate` now, not a list of archives.
 *
 * ⚠️ The rules these fixtures used to prove in the browser — records count
 * completed prints only, "most expensive" ranks on filament plus measured
 * electricity, materials split "PLA, PETG" with grams divided and counts whole
 * — live in SQL now and are pinned in
 * backend/tests/unit/services/test_archive_aggregate_queries.py. What is left
 * to prove here is that the page RENDERS what the server decided.
 */
const emptyMetrics = {
  prints: 0, completed: 0, failed: 0, grams: 0,
  cost: 0, energy_cost: 0, quantity: 0, seconds: 0,
};

function aggregate(overrides: Record<string, unknown> = {}) {
  return {
    timezone: 'UTC',
    granularity: 'day',
    buckets: [],
    by_hour_of_day: Array.from({ length: 24 }, (_, hour) => ({ hour, prints: 0, failures: 0 })),
    by_printer: [],
    by_material: [],
    by_color: [],
    by_duration: [],
    totals: { ...emptyMetrics, energy_kwh: 0, printers: 0 },
    records: { longest: null, heaviest: null, costliest: null, success_streak: 0 },
    ...overrides,
  };
}

function bucket(at: string, started: Partial<typeof emptyMetrics>, ended: Partial<typeof emptyMetrics>) {
  return { at, started: { ...emptyMetrics, ...started }, ended: { ...emptyMetrics, ...ended } };
}

// A small farm's week: three completed prints and one failure.
const mockAggregate = aggregate({
  buckets: [
    bucket('2024-01-01', { prints: 1, completed: 1, grams: 25, seconds: 16200 },
                         { prints: 1, completed: 1, grams: 25, cost: 0.75, quantity: 1, seconds: 16200 }),
    bucket('2024-01-02', { prints: 1, completed: 1, grams: 180, seconds: 28800 },
                         { prints: 1, completed: 1, grams: 180, cost: 5.4, quantity: 1, seconds: 28800 }),
    bucket('2024-01-03', { prints: 2, completed: 1, failed: 1, grams: 55, seconds: 25200 },
                         { prints: 1, failed: 1, grams: 10, cost: 0.3, quantity: 1, seconds: 3600 }),
    bucket('2024-01-04', {}, { prints: 1, completed: 1, grams: 45, cost: 1.35, quantity: 1, seconds: 21600 }),
  ],
  by_printer: [
    { printer_id: 1, prints: 2, grams: 205, seconds: 45000, completed: 2, failed: 0 },
    { printer_id: 2, prints: 2, grams: 55, seconds: 25200, completed: 1, failed: 1 },
  ],
  by_material: [
    { material: 'PETG', prints: 1, grams: 180, seconds: 28800, completed: 1, failed: 0 },
    { material: 'PLA', prints: 2, grams: 70, seconds: 37800, completed: 2, failed: 0 },
    { material: 'ABS', prints: 1, grams: 10, seconds: 3600, completed: 0, failed: 1 },
  ],
  by_color: [
    { color: '#FF0000', prints: 1, grams: 180 },
    { color: '#00FF00', prints: 2, grams: 70 },
    { color: '#0000FF', prints: 1, grams: 10 },
  ],
  by_duration: [
    { bucket: '<30m', prints: 0 }, { bucket: '30m-1h', prints: 0 }, { bucket: '1-2h', prints: 1 },
    { bucket: '2-4h', prints: 0 }, { bucket: '4-8h', prints: 3 }, { bucket: '8-12h', prints: 0 },
    { bucket: '12-24h', prints: 0 }, { bucket: '24h+', prints: 0 },
  ],
  totals: {
    prints: 4, completed: 3, failed: 1, grams: 260, cost: 7.8,
    energy_kwh: 0, energy_cost: 0, quantity: 4, seconds: 70200, printers: 2,
  },
  records: {
    longest: { archive_id: 2, print_name: 'Large Vase', seconds: 28800 },
    heaviest: { archive_id: 2, print_name: 'Large Vase', grams: 180 },
    costliest: { archive_id: 2, print_name: 'Large Vase', total: 5.4, cost: 5.4, energy_cost: 0 },
    success_streak: 2,
  },
});

const mockSettings = {
  currency: 'USD',
  check_updates: false,
  check_printer_firmware: false,
};

const mockFailureAnalysis = {
  period_days: 30,
  total_prints: 100,
  failed_prints: 5,
  failure_rate: 5.0,
  failures_by_reason: {
    'First layer adhesion': 3,
    'Filament runout': 2,
  },
  failures_by_filament: {
    'ABS': 3,
    'PLA': 2,
  },
  failures_by_printer: {
    '1': 2,
    '2': 3,
  },
  failures_by_hour: {},
  recent_failures: [],
  trend: [
    { week_start: '2024-01-01', total_prints: 50, failed_prints: 3, failure_rate: 6.0 },
    { week_start: '2024-01-08', total_prints: 50, failed_prints: 2, failure_rate: 5.0 },
  ],
};

describe('StatsPage', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/statistics/overview', () => {
        return HttpResponse.json(mockStats);
      }),
      http.get('/api/v1/printers/', () => {
        return HttpResponse.json(mockPrinters);
      }),
      http.get('/api/v1/statistics/aggregate', () => {
        return HttpResponse.json(mockAggregate);
      }),
      http.get('/api/v1/settings/', () => {
        return HttpResponse.json(mockSettings);
      }),
      http.get('/api/v1/statistics/failures', () => {
        return HttpResponse.json(mockFailureAnalysis);
      })
    );
  });

  describe('rendering', () => {
    it('renders the page title', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Dashboard')).toBeInTheDocument();
      });
    });

    it('shows quick stats widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Quick Stats')).toBeInTheDocument();
      });
    });

    it('shows total prints stat', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Total Prints')).toBeInTheDocument();
        expect(screen.getByText('150')).toBeInTheDocument();
      });
    });

    it('shows print time stat', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Print Time')).toBeInTheDocument();
        expect(screen.getByText('500.5h')).toBeInTheDocument();
      });
    });

    it('shows filament used stat', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Filament Used')).toBeInTheDocument();
        expect(screen.getByText('5.5kg')).toBeInTheDocument();
      });
    });
  });

  describe('success rate', () => {
    it('shows success rate widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Success Rate')).toBeInTheDocument();
        // Success rate: 140/(140+10) = 93%
        expect(screen.getByText('93%')).toBeInTheDocument();
      });
    });
  });

  describe('cost display', () => {
    it('shows filament cost', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Filament Cost')).toBeInTheDocument();
      });
    });

    it('shows both energy costs — printing, and everything the plugs counted', async () => {
      // ⚠️ Two pairs since the display-mode setting went. One pair whose
      // meaning depended on a setting could not be read without opening
      // Settings to find out which question it had answered.
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Cost While Printing')).toBeInTheDocument();
      });
      expect(screen.getByText('Cost At The Plug')).toBeInTheDocument();
      expect(screen.getByText('Energy While Printing')).toBeInTheDocument();
      expect(screen.getByText('Energy At The Plug')).toBeInTheDocument();
    });
  });

  describe('widgets', () => {
    it('shows time accuracy widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Time Accuracy')).toBeInTheDocument();
      });
    });

    it('shows print activity widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Print Activity')).toBeInTheDocument();
      });
    });

    it('shows failure analysis widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Failure Analysis')).toBeInTheDocument();
      });
    });

    it('translates camelCase failure-reason keys (#1687 follow-up)', async () => {
      server.use(
        http.get('/api/v1/statistics/failures', () => {
          return HttpResponse.json({
            ...mockFailureAnalysis,
            failures_by_reason: { filamentRunout: 3, cloggedNozzle: 1 },
          });
        })
      );
      render(<StatsPage />);

      // The raw camelCase key must render as its translated label, not the key.
      expect(await screen.findByText('Filament runout')).toBeInTheDocument();
      expect(screen.queryByText('filamentRunout')).not.toBeInTheDocument();
    });

    it('renders legacy translated-text failure reasons unchanged', async () => {
      server.use(
        http.get('/api/v1/statistics/failures', () => {
          return HttpResponse.json({
            ...mockFailureAnalysis,
            failures_by_reason: { 'First layer adhesion': 2 },
          });
        })
      );
      render(<StatsPage />);

      // Unknown key falls through to defaultValue → legacy text renders as-is.
      expect(await screen.findByText('First layer adhesion')).toBeInTheDocument();
    });

    it('shows printer stats widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Printer Stats')).toBeInTheDocument();
      });
    });

    it('shows filament trends widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Filament Trends')).toBeInTheDocument();
      });
    });

    it('shows records widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Records')).toBeInTheDocument();
      });
    });
  });

  describe('printer stats sub-cards', () => {
    it('shows prints by printer section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Prints by Printer')).toBeInTheDocument();
      });
    });

    it('shows print duration section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Print Duration')).toBeInTheDocument();
      });
    });

    it('shows print habits section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Print Habits')).toBeInTheDocument();
      });
    });

    it('shows print time of day section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Print Time of Day')).toBeInTheDocument();
      });
    });
  });

  describe('defects by printer', () => {
    it('lists defects by printer, worst rate first', async () => {
      server.use(
        http.get('/api/v1/statistics/overview', () =>
          HttpResponse.json({
            total_prints: 3, successful_prints: 3, failed_prints: 0, cancelled_prints: 0,
            total_print_time_hours: 1, total_filament_grams: 10, total_cost: 1,
            prints_by_filament_type: {}, prints_by_printer: { '1': 2, '2': 1 },
            average_time_accuracy: null, time_accuracy_by_printer: null,
            print_energy_kwh: 0, print_energy_cost: 0, total_energy_kwh: 0, total_energy_cost: 0,
            defects_by_printer: { '1': { printed: 10, defective: 1 }, '2': { printed: 4, defective: 2 } },
          }),
        ),
      );
      render(<StatsPage />);

      const table = await screen.findByTestId('defects-by-printer');
      const rows = within(table).getAllByRole('row').slice(1); // header first
      expect(rows[0].textContent).toContain('50.0%');
      expect(rows[1].textContent).toContain('10.0%');
    });
  });

  describe('filament trends sub-cards', () => {
    it('shows by material section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('By Material')).toBeInTheDocument();
      });
    });

    it('shows success by material section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Success by Material')).toBeInTheDocument();
      });
    });

    it('shows color distribution section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Color Distribution')).toBeInTheDocument();
      });
    });
  });

  describe('records widget', () => {
    it('shows longest print record', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Longest Print')).toBeInTheDocument();
      });
    });

    it('shows heaviest print record', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Heaviest Print')).toBeInTheDocument();
      });
    });

    it('shows most expensive record', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Most Expensive')).toBeInTheDocument();
      });
    });

    it('shows success streak record', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Success Streak')).toBeInTheDocument();
      });
    });

    it('renders the records the server decided, with the cost split', async () => {
      // ⚠️ The RULES behind these numbers moved into SQL: completed prints only,
      // "most expensive" ranked on filament plus measured electricity. They are
      // pinned in backend/tests/unit/services/test_archive_aggregate_queries.py.
      // What this page still owns is showing the split, so a total can be
      // reconciled against the print's own page instead of reading as a wrong
      // filament cost.
      server.use(
        http.get('/api/v1/statistics/aggregate', () =>
          HttpResponse.json(
            aggregate({
              totals: {
                prints: 2, completed: 2, failed: 0, grams: 50, cost: 9.0,
                energy_kwh: 8.1, energy_cost: 3.25, quantity: 2, seconds: 39600, printers: 1,
              },
              records: {
                longest: { archive_id: 21, print_name: 'Long And Hungry', seconds: 36000 },
                heaviest: { archive_id: 21, print_name: 'Long And Hungry', grams: 30 },
                costliest: {
                  archive_id: 21, print_name: 'Long And Hungry',
                  total: 7.2, cost: 4.0, energy_cost: 3.2,
                },
                success_streak: 2,
              },
            }),
          ),
        ),
      );
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Most Expensive')).toBeInTheDocument();
      });
      expect(screen.getByText('$7.20')).toBeInTheDocument();
      expect(screen.getByText(/filament \$4\.00 \+ power \$3\.20/)).toBeInTheDocument();
      expect(screen.getAllByText('Long And Hungry').length).toBeGreaterThan(0);
    });

    it('shows no cost breakdown when nothing was measured', async () => {
      // An unmetered print carries zero energy and competes on filament alone.
      // A "+ power $0.00" would claim it ran on no electricity, which is a
      // different statement from "we did not measure it".
      server.use(
        http.get('/api/v1/statistics/aggregate', () =>
          HttpResponse.json(
            aggregate({
              totals: {
                prints: 1, completed: 1, failed: 0, grams: 20, cost: 6.0,
                energy_kwh: 0, energy_cost: 0, quantity: 1, seconds: 3600, printers: 1,
              },
              records: {
                longest: null,
                heaviest: null,
                costliest: { archive_id: 30, print_name: 'Unmetered', total: 6.0, cost: 6.0, energy_cost: 0 },
                success_streak: 1,
              },
            }),
          ),
        ),
      );
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Most Expensive')).toBeInTheDocument();
      });
      expect(screen.getAllByText('$6.00').length).toBeGreaterThan(0);
      expect(screen.queryByText(/power \$0\.00/)).not.toBeInTheDocument();
    });

    it('shows no records at all when the range is empty', async () => {
      server.use(http.get('/api/v1/statistics/aggregate', () => HttpResponse.json(aggregate())));
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Records')).toBeInTheDocument();
      });
      expect(screen.queryByText('Most Expensive')).not.toBeInTheDocument();
      expect(screen.queryByText('Success Streak')).not.toBeInTheDocument();
    });
  });

  describe('export', () => {
    it('has export button', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Export Stats')).toBeInTheDocument();
      });
    });
  });

  describe('recalculate costs', () => {
    it('has recalculate costs button', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Recalculate Costs')).toBeInTheDocument();
      });
    });
  });
});

describe('while the numbers are still loading', () => {
  /**
   * ⚠️ The page used to be an early return: one line of centred text in place
   * of EVERYTHING — title, timeframe picker, export buttons. On a farm with a
   * long archive it looked broken for as long as the query took, and the
   * controls that could have narrowed the range were exactly the part you
   * could not reach.
   */
  beforeEach(() => {
    server.use(
      // Never resolves: the page has to be usable in this state, not merely
      // survive it.
      http.get('/api/v1/statistics/overview', () => new Promise(() => {})),
    );
  });

  it('draws the title straight away', async () => {
    render(<StatsPage />);

    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument();
  });

  it('offers the controls that would narrow the query', async () => {
    render(<StatsPage />);

    expect(await screen.findByRole('button', { name: /reset layout/i })).toBeInTheDocument();
  });

  it('says it is working rather than looking empty', async () => {
    render(<StatsPage />);

    expect(await screen.findByText('Loading statistics...')).toBeInTheDocument();
  });
});
