/**
 * Tests for the StatsPage component.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
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

const mockArchives = [
  {
    id: 1,
    created_at: '2024-01-01T10:00:00Z',
    started_at: '2024-01-01T10:00:00Z',
    completed_at: '2024-01-01T14:30:00Z',
    print_name: 'Benchy',
    status: 'completed',
    printer_id: 1,
    filament_type: 'PLA',
    filament_color: '#00FF00',
    filament_used_grams: 25,
    actual_time_seconds: 16200,
    print_time_seconds: 15000,
    cost: 0.75,
    quantity: 1,
  },
  {
    id: 2,
    created_at: '2024-01-02T14:00:00Z',
    started_at: '2024-01-02T14:00:00Z',
    completed_at: '2024-01-02T22:00:00Z',
    print_name: 'Large Vase',
    status: 'completed',
    printer_id: 1,
    filament_type: 'PETG',
    filament_color: '#FF0000',
    filament_used_grams: 180,
    actual_time_seconds: 28800,
    print_time_seconds: 27000,
    cost: 5.40,
    quantity: 1,
  },
  {
    id: 3,
    created_at: '2024-01-03T08:00:00Z',
    started_at: '2024-01-03T08:00:00Z',
    completed_at: null,
    print_name: 'Failed Bracket',
    status: 'failed',
    printer_id: 2,
    filament_type: 'ABS',
    filament_color: '#0000FF',
    filament_used_grams: 10,
    actual_time_seconds: 3600,
    print_time_seconds: 7200,
    cost: 0.30,
    quantity: 1,
  },
  {
    id: 4,
    created_at: '2024-01-03T20:00:00Z',
    started_at: '2024-01-03T20:00:00Z',
    completed_at: '2024-01-04T02:00:00Z',
    print_name: 'Phone Stand',
    status: 'completed',
    printer_id: 2,
    filament_type: 'PLA',
    filament_color: '#00FF00',
    filament_used_grams: 45,
    actual_time_seconds: 21600,
    print_time_seconds: 20000,
    cost: 1.35,
    quantity: 1,
  },
];

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
      http.get('/api/v1/archives/stats', () => {
        return HttpResponse.json(mockStats);
      }),
      http.get('/api/v1/printers/', () => {
        return HttpResponse.json(mockPrinters);
      }),
      http.get('/api/v1/archives/slim', () => {
        return HttpResponse.json(mockArchives);
      }),
      http.get('/api/v1/settings/', () => {
        return HttpResponse.json(mockSettings);
      }),
      http.get('/api/v1/archives/analysis/failures', () => {
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
        http.get('/api/v1/archives/analysis/failures', () => {
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
        http.get('/api/v1/archives/analysis/failures', () => {
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

    it('excludes non-completed prints from records', async () => {
      // A failed print with outlier time/weight/cost must NOT win any record.
      server.use(
        http.get('/api/v1/archives/slim', () =>
          HttpResponse.json([
            {
              id: 10, created_at: '2024-02-01T10:00:00Z', started_at: '2024-02-01T10:00:00Z',
              completed_at: '2024-02-01T12:00:00Z', print_name: 'Good Small', status: 'completed',
              printer_id: 1, filament_type: 'PLA', filament_color: '#00FF00',
              filament_used_grams: 20, actual_time_seconds: 7200, print_time_seconds: 7000, cost: 2.0, quantity: 1,
            },
            {
              id: 11, created_at: '2024-02-02T10:00:00Z', started_at: '2024-02-02T10:00:00Z',
              completed_at: null, print_name: 'Failed Big', status: 'failed',
              printer_id: 1, filament_type: 'ABS', filament_color: '#0000FF',
              filament_used_grams: 999, actual_time_seconds: 99999, print_time_seconds: 99999, cost: 99.0, quantity: 1,
            },
          ])
        )
      );
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Most Expensive')).toBeInTheDocument();
      });
      // The failed print's outlier values must never surface as a record.
      expect(screen.queryByText('Failed Big')).not.toBeInTheDocument();
      expect(screen.queryByText('$99.00')).not.toBeInTheDocument();
      // The completed print is the record instead.
      expect(screen.getAllByText('Good Small').length).toBeGreaterThan(0);
    });

    it('ranks Most Expensive on filament AND measured electricity', async () => {
      // The load-bearing case: the print with the DEARER FILAMENT loses once the
      // power it drew is counted. Ranking on `cost` alone answered a narrower
      // question than the label promises — `cost` is filament only.
      server.use(
        http.get('/api/v1/archives/slim', () =>
          HttpResponse.json([
            {
              id: 20, created_at: '2024-03-01T10:00:00Z', started_at: '2024-03-01T10:00:00Z',
              completed_at: '2024-03-01T11:00:00Z', print_name: 'Pricey Filament', status: 'completed',
              printer_id: 1, filament_type: 'PLA', filament_color: '#00FF00',
              filament_used_grams: 20, actual_time_seconds: 3600, print_time_seconds: 3600,
              cost: 5.0, energy_kwh: 0.1, energy_cost: 0.05, quantity: 1,
            },
            {
              id: 21, created_at: '2024-03-02T10:00:00Z', started_at: '2024-03-02T10:00:00Z',
              completed_at: '2024-03-02T20:00:00Z', print_name: 'Long And Hungry', status: 'completed',
              printer_id: 1, filament_type: 'ABS', filament_color: '#0000FF',
              filament_used_grams: 30, actual_time_seconds: 36000, print_time_seconds: 36000,
              cost: 4.0, energy_kwh: 8.0, energy_cost: 3.2, quantity: 1,
            },
          ])
        )
      );
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Most Expensive')).toBeInTheDocument();
      });
      // 4.00 filament + 3.20 power = 7.20 beats 5.00 + 0.05 = 5.05.
      expect(screen.getByText('$7.20')).toBeInTheDocument();
      expect(screen.queryByText('$5.05')).not.toBeInTheDocument();
      // And the split is shown, so the total can be reconciled against the
      // print's own page rather than reading as a wrong filament cost.
      expect(screen.getByText(/filament \$4\.00 \+ power \$3\.20/)).toBeInTheDocument();
    });

    it('lets a print with no plug compete on filament alone', async () => {
      // Unmetered prints carry null energy. They must still be rankable —
      // treating "not measured" as disqualifying would hide every record on a
      // farm without smart plugs.
      server.use(
        http.get('/api/v1/archives/slim', () =>
          HttpResponse.json([
            {
              id: 30, created_at: '2024-04-01T10:00:00Z', started_at: '2024-04-01T10:00:00Z',
              completed_at: '2024-04-01T11:00:00Z', print_name: 'Unmetered', status: 'completed',
              printer_id: 1, filament_type: 'PLA', filament_color: '#00FF00',
              filament_used_grams: 20, actual_time_seconds: 3600, print_time_seconds: 3600,
              cost: 6.0, energy_kwh: null, energy_cost: null, quantity: 1,
            },
          ])
        )
      );
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Most Expensive')).toBeInTheDocument();
      });
      // getAllByText: with one archive its cost is also the period total, so
      // the same figure renders in more than one widget.
      expect(screen.getAllByText('$6.00').length).toBeGreaterThan(0);
      // No breakdown when nothing was measured — a "+ power $0.00" would claim
      // the print ran on no electricity.
      expect(screen.queryByText(/power \$0\.00/)).not.toBeInTheDocument();
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
