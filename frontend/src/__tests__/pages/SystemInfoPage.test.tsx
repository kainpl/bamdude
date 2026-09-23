/**
 * Tests for the SystemInfoPage component.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { SystemInfoPage } from '../../pages/SystemInfoPage';
import { api } from '../../api/client';
import i18n from '../../i18n';

// Mock the API client
vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getSystemInfo: vi.fn(),
    getDatabaseHealth: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    updateSettings: vi.fn().mockResolvedValue({}),
    // Defaults to disabled so the existing tests see the page they always saw:
    // the Zigbee section is absent unless an install actually uses Zigbee.
    getZigbeeStatus: vi.fn().mockResolvedValue({
      state: 'disabled',
      reason: null,
      coordinator: null,
      network: null,
      radio_changed: null,
    }),
    getZigbeeDevices: vi.fn().mockResolvedValue({ devices: [] }),
  },
  supportApi: {
    getDebugLoggingState: vi.fn().mockResolvedValue({ enabled: false, enabled_at: null, duration_seconds: null }),
    setDebugLogging: vi.fn().mockResolvedValue({ enabled: true, enabled_at: new Date().toISOString(), duration_seconds: 0 }),
    downloadSupportBundle: vi.fn().mockResolvedValue(undefined),
  },
}));

/** GET /system/database — a healthy bundled PostgreSQL with one slow query. */
const mockDbHealth = {
  engine: 'PostgreSQL',
  version: '18.6',
  mode: 'embedded' as const,
  size_bytes: 734_003_200,
  pool: {
    dialect: 'postgresql',
    config: {},
    current_size: 20,
    checked_out: 3,
    checked_in: 17,
    overflow: 0,
  },
  instrumentation: {
    query_threshold_ms: 500,
    request_threshold_ms: 3000,
    source: 'pg_stat_statements' as const,
    reason: null,
    slowest: [
      { statement: 'SELECT * FROM print_archives WHERE printer_id = $1', count: 42, total_ms: 51_200, mean_ms: 1219 },
    ],
  },
  sqlite: null,
  postgres: {
    connections: { used: 12, max: 200 },
    cache_hit_ratio: 0.9932,
    commits: 100,
    rollbacks: 1,
    deadlocks: 0,
    temp_files: 0,
    temp_bytes: 0,
  },
  largest_tables: [
    { table: 'print_archives', bytes: 512_000_000, rows: 184_320 },
    { table: 'spool_usage_history', bytes: 96_000_000, rows: 902_100 },
  ],
  scans: [{ table: 'print_archives', seq_scan: 41_200, idx_scan: 12 }],
  probes_failed: [],
};

// Mock system info response
const mockSystemInfo = {
  app: {
    version: '0.1.5b',
    base_dir: '/opt/bamdude',
    archive_dir: '/opt/bamdude/archives',
    started_at: '2024-12-11T02:00:00+00:00',
    uptime_seconds: 7200,
    uptime_formatted: '2h',
  },
  database: {
    archives: 150,
    archives_completed: 140,
    archives_failed: 8,
    archives_printing: 2,
    printers: 3,
    filaments: 25,
    projects: 5,
    smart_plugs: 2,
    total_print_time_seconds: 360000,
    total_print_time_formatted: '100h',
    total_filament_grams: 5000,
    total_filament_kg: 5.0,
  },
  printers: {
    total: 3,
    connected: 2,
    connected_list: [
      { id: 1, name: 'X1C-01', state: 'IDLE', model: 'X1C' },
      { id: 2, name: 'P1S-01', state: 'RUNNING', model: 'P1S' },
    ],
  },
  storage: {
    archive_size_bytes: 1073741824,
    archive_size_formatted: '1.0 GB',
    database_size_bytes: 10485760,
    database_size_formatted: '10.0 MB',
    disk_total_bytes: 107374182400,
    disk_total_formatted: '100.0 GB',
    disk_used_bytes: 53687091200,
    disk_used_formatted: '50.0 GB',
    disk_free_bytes: 53687091200,
    disk_free_formatted: '50.0 GB',
    disk_percent_used: 50.0,
  },
  system: {
    platform: 'Linux',
    platform_release: '5.15.0',
    platform_version: '#1 SMP',
    architecture: 'x86_64',
    hostname: 'bamdude-server',
    python_version: '3.11.0',
    uptime_seconds: 86400,
    uptime_formatted: '1d',
    boot_time: '2024-12-11T00:00:00',
  },
  memory: {
    total_bytes: 17179869184,
    total_formatted: '16.0 GB',
    available_bytes: 8589934592,
    available_formatted: '8.0 GB',
    used_bytes: 8589934592,
    used_formatted: '8.0 GB',
    percent_used: 50.0,
  },
  cpu: {
    count: 4,
    count_logical: 8,
    percent: 25.0,
  },
};

describe('SystemInfoPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows recovery instructions and the safe runtime path without a repair button', async () => {
    vi.mocked(api.getSystemInfo).mockResolvedValue({
      ...mockSystemInfo,
      preview: {
        state: 'unavailable', reason: 'recovery_required', error_type: 'RecoveryRequired',
        runtime_dir: '/data/.cache/preview-service', recovery_required: true,
      },
    } as never);
    render(<SystemInfoPage />);
    expect(await screen.findByText('RecoveryRequired')).toBeInTheDocument();
    expect(screen.getByText('/data/.cache/preview-service')).toBeInTheDocument();
    expect(screen.getByText(/Do not delete the runtime marker blindly/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Preview troubleshooting and recovery' })).toHaveAttribute(
      'href', 'https://docs.bamdude.top/reference/troubleshooting/#local-preview-service',
    );
    expect(screen.queryByRole('button', { name: /recover|repair/i })).not.toBeInTheDocument();
  });

  it('renders a recovering worker separately from manual recovery', async () => {
    vi.mocked(api.getSystemInfo).mockResolvedValue({
      ...mockSystemInfo,
      preview: {
        state: 'recovering', reason: 'circuit_open', error_type: null,
        runtime_dir: '/data/.cache/preview-service', recovery_required: false,
      },
    } as never);
    render(<SystemInfoPage />);
    expect(await screen.findByText('Restarting')).toBeInTheDocument();
    expect(screen.getByText(/automatic restart is temporarily delayed/)).toBeInTheDocument();
    expect(screen.queryByText(/Do not delete the runtime marker blindly/)).not.toBeInTheDocument();
  });

  it('does not display recovery advice for a healthy service', async () => {
    vi.mocked(api.getSystemInfo).mockResolvedValue({
      ...mockSystemInfo,
      preview: { state: 'ready', reason: null, error_type: null, runtime_dir: '/cache', recovery_required: false },
    } as never);
    render(<SystemInfoPage />);
    expect(await screen.findByText('Local preview service')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Preview troubleshooting and recovery' })).not.toBeInTheDocument();
  });

  it('uses the Ukrainian recovery text and matching documentation page', async () => {
    vi.mocked(api.getSystemInfo).mockResolvedValue({
      ...mockSystemInfo,
      preview: {
        state: 'unavailable', reason: 'recovery_required', error_type: 'RecoveryRequired',
        runtime_dir: '/cache', recovery_required: true,
      },
    } as never);
    await i18n.changeLanguage('uk');
    const view = render(<SystemInfoPage />);
    try {
      expect(await screen.findByText('Локальний сервіс прев’ю')).toBeInTheDocument();
      expect(screen.getByText(/Не видаляйте маркер середовища навмання/)).toBeInTheDocument();
      expect(screen.getByRole('link', { name: 'Діагностика та відновлення прев’ю' })).toHaveAttribute(
        'href', 'https://docs.bamdude.top/uk/reference/troubleshooting/#local-preview-service',
      );
    } finally {
      view.unmount();
      await i18n.changeLanguage('en');
    }
  });

  it('renders loading state initially', async () => {
    // Make the API call never resolve to test loading state
    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise(() => {})
    );

    render(<SystemInfoPage />);

    // Should show loading spinner
    expect(document.querySelector('.animate-spin')).toBeInTheDocument();
  });

  it('renders system info when data loads', async () => {
    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(mockSystemInfo);

    render(<SystemInfoPage />);

    // ⚠️ Waits for a NUMBER, not for the title. The title is drawn before the
    // query resolves now — that is the point of the split — so waiting on it
    // returns immediately and the assertion below would race the data it needs.
    expect(await screen.findByText('v0.1.5b')).toBeInTheDocument();
    expect(screen.getByText('System Information')).toBeInTheDocument();
  });

  it('displays application section', async () => {
    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(mockSystemInfo);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getByText('Application')).toBeInTheDocument();
    });

    expect(screen.getByText('v0.1.5b')).toBeInTheDocument();
    expect(screen.getByText('bamdude-server')).toBeInTheDocument();
    expect(screen.getByText('1d')).toBeInTheDocument();
    expect(screen.getByText('BamDude uptime')).toBeInTheDocument();
    expect(screen.getByText('BamDude started')).toBeInTheDocument();
    expect(screen.getByText('System uptime')).toBeInTheDocument();
    expect(screen.getByText('System started')).toBeInTheDocument();
    expect(screen.getByText('2h')).toBeInTheDocument();
  });

  it('displays database statistics', async () => {
    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(mockSystemInfo);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getByText('Database')).toBeInTheDocument();
    });

    // Check archive counts
    expect(screen.getByText('150')).toBeInTheDocument(); // Total archives
    expect(screen.getByText('140')).toBeInTheDocument(); // Completed
    expect(screen.getByText('8')).toBeInTheDocument(); // Failed
  });

  it('displays connected printers', async () => {
    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(mockSystemInfo);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getByText('Connected Printers')).toBeInTheDocument();
    });

    // Check connected printer names
    expect(screen.getByText('X1C-01')).toBeInTheDocument();
    expect(screen.getByText('P1S-01')).toBeInTheDocument();

    // Check printer states
    expect(screen.getByText('IDLE')).toBeInTheDocument();
    expect(screen.getByText('RUNNING')).toBeInTheDocument();
  });

  it('displays storage information', async () => {
    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(mockSystemInfo);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getByText('Storage')).toBeInTheDocument();
    });

    expect(screen.getByText('1.0 GB')).toBeInTheDocument(); // Archive size
    expect(screen.getByText('10.0 MB')).toBeInTheDocument(); // Database size
  });

  it('displays memory usage', async () => {
    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(mockSystemInfo);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getByText('Memory')).toBeInTheDocument();
    });

    expect(screen.getByText('8.0 GB available')).toBeInTheDocument();
  });

  it('displays CPU information', async () => {
    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(mockSystemInfo);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getByText('CPU')).toBeInTheDocument();
    });

    expect(screen.getByText('4')).toBeInTheDocument(); // CPU cores
    expect(screen.getByText('25%')).toBeInTheDocument(); // CPU usage
  });

  it('displays system details', async () => {
    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(mockSystemInfo);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getByText('System Details')).toBeInTheDocument();
    });

    expect(screen.getByText('Linux')).toBeInTheDocument();
    expect(screen.getByText('x86_64')).toBeInTheDocument();
    expect(screen.getByText('3.11.0')).toBeInTheDocument(); // Python version
  });

  it('shows error state when data fails to load', async () => {
    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(null);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
    });
  });

  it('shows no printers message when none connected', async () => {
    const noConnectedPrinters = {
      ...mockSystemInfo,
      printers: {
        total: 3,
        connected: 0,
        connected_list: [],
      },
    };

    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(noConnectedPrinters);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getByText(/no printers connected/i)).toBeInTheDocument();
    });
  });

  it('has refresh button', async () => {
    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(mockSystemInfo);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getAllByText('Refresh').length).toBeGreaterThan(0);
    });
  });

  it('applies warning color for high disk usage', async () => {
    const highDiskUsage = {
      ...mockSystemInfo,
      storage: {
        ...mockSystemInfo.storage,
        disk_percent_used: 80,
      },
    };

    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(highDiskUsage);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getByText('Storage')).toBeInTheDocument();
    });

    // The progress bar should have yellow color for 75-90% usage
    const progressBars = document.querySelectorAll('[class*="bg-yellow"]');
    expect(progressBars.length).toBeGreaterThan(0);
  });

  it('displays extended privacy disclosure items', async () => {
    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(mockSystemInfo);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getByText("What's in the support bundle?")).toBeInTheDocument();
    });

    // Original items
    expect(screen.getByText(/App version and debug mode/)).toBeInTheDocument();
    expect(screen.getByText(/Debug logs \(sanitized\)/)).toBeInTheDocument();

    // New diagnostic items
    expect(screen.getByText(/Printer connectivity and firmware versions/)).toBeInTheDocument();
    expect(screen.getByText(/Integration status \(Spoolman, MQTT, HA\)/)).toBeInTheDocument();
    expect(screen.getByText(/Network interfaces \(subnets only\)/)).toBeInTheDocument();
    expect(screen.getByText(/Python package versions/)).toBeInTheDocument();
    expect(screen.getByText(/Database health checks/)).toBeInTheDocument();
    expect(screen.getByText(/Docker environment details/)).toBeInTheDocument();
  });

  it('applies danger color for critical disk usage', async () => {
    const criticalDiskUsage = {
      ...mockSystemInfo,
      storage: {
        ...mockSystemInfo.storage,
        disk_percent_used: 95,
      },
    };

    (api.getSystemInfo as ReturnType<typeof vi.fn>).mockResolvedValue(criticalDiskUsage);

    render(<SystemInfoPage />);

    await waitFor(() => {
      expect(screen.getByText('Storage')).toBeInTheDocument();
    });

    // The progress bar should have red color for >90% usage
    const progressBars = document.querySelectorAll('[class*="bg-red"]');
    expect(progressBars.length).toBeGreaterThan(0);
  });
});

describe('SystemInfoPage Zigbee diagnostics', () => {
  /**
   * The coordinator belongs on a diagnostics page for the same reason the
   * attribute dump exists: when readings look wrong, the first questions are
   * which dongle is answering and on which network. Both were unreachable
   * outside the settings tab.
   */
  beforeEach(() => {
    vi.mocked(api.getSystemInfo).mockResolvedValue(mockSystemInfo as never);
    vi.mocked(api.getDatabaseHealth).mockResolvedValue(mockDbHealth);
  });

  it('shows the radio identity when the coordinator is up', async () => {
    vi.mocked(api.getZigbeeStatus).mockResolvedValue({
      state: 'up',
      reason: null,
      coordinator: {
        ieee: '34:8d:13:ff:fe:11:e4:6f',
        nwk: 0,
        model: 'Dongle-M',
        manufacturer: 'SONOFF',
        version: '7.4.5.0',
      },
      network: { channel: 25, pan_id: 30710 },
      radio_changed: null,
    });
    vi.mocked(api.getZigbeeDevices).mockResolvedValue({ devices: [] });

    render(<SystemInfoPage />);

    expect(await screen.findByText('34:8d:13:ff:fe:11:e4:6f')).toBeInTheDocument();
  });

  it('shows the failure reason verbatim when it is down', async () => {
    vi.mocked(api.getZigbeeStatus).mockResolvedValue({
      state: 'error',
      reason: 'TransientConnectionError',
      coordinator: null,
      network: null,
      radio_changed: null,
    });
    vi.mocked(api.getZigbeeDevices).mockResolvedValue({ devices: [] });

    render(<SystemInfoPage />);

    expect(await screen.findByText('TransientConnectionError')).toBeInTheDocument();
  });

  it('omits the whole section when Zigbee is disabled', async () => {
    vi.mocked(api.getZigbeeStatus).mockResolvedValue({
      state: 'disabled',
      reason: null,
      coordinator: null,
      network: null,
      radio_changed: null,
    });
    vi.mocked(api.getZigbeeDevices).mockResolvedValue({ devices: [] });

    render(<SystemInfoPage />);

    // A diagnostics page should not carry a permanent entry about a feature the
    // install does not use.
    await screen.findByText(/0\.1\.5b/);
    expect(screen.queryByText(/zigbee/i)).not.toBeInTheDocument();
  });

  describe('database health', () => {
    it('shows the engine, the mode and the pool', async () => {
      render(<SystemInfoPage />);

      expect(await screen.findByText('PostgreSQL 18.6')).toBeInTheDocument();
      // ⚠️ The mode is not the dialect: DATABASE_URL=embedded reaches the engine
      // as a postgresql:// URL, so this line is the only place the difference
      // between "BamDude runs it" and "somebody else does" is visible.
      expect(screen.getByText(/run by BamDude/i)).toBeInTheDocument();
      expect(screen.getByText('3 / 20')).toBeInTheDocument();
      expect(screen.getByText('99.3%')).toBeInTheDocument();
    });

    it('lists the slowest statements', async () => {
      render(<SystemInfoPage />);
      expect(
        await screen.findByText('SELECT * FROM print_archives WHERE printer_id = $1'),
      ).toBeInTheDocument();
    });

    it('says instrumentation is off instead of showing an empty table', async () => {
      vi.mocked(api.getDatabaseHealth).mockResolvedValue({
        ...mockDbHealth,
        instrumentation: {
          query_threshold_ms: 0,
          request_threshold_ms: 0,
          source: 'in_process' as const,
          reason: 'Slow-query logging is off. Set slow_query_ms in Settings to start collecting.',
          slowest: [],
        },
      });
      render(<SystemInfoPage />);
      expect(await screen.findByText(/slow_query_ms/)).toBeInTheDocument();
    });

    it('shows what PostgreSQL knows about where its space went', async () => {
      // The same question pgAdmin answers, and the reason the size figure had
      // to stop being a file stat.
      render(<SystemInfoPage />);

      expect(await screen.findByText('print_archives')).toBeInTheDocument();
      expect(screen.getByText('488.3 MB')).toBeInTheDocument();
      // Sequential scans beside index scans: the shape that wants an index.
      // Tolerant of the runtime's thousands separator, which is not what
      // this test is about.
      expect(screen.getByText(/41.200\s*\/\s*12/)).toBeInTheDocument();
    });

    it('names the probes that could not be read', async () => {
      vi.mocked(api.getDatabaseHealth).mockResolvedValue({
        ...mockDbHealth,
        size_bytes: null,
        probes_failed: ['size_bytes'],
      });
      render(<SystemInfoPage />);
      expect(await screen.findByText(/size_bytes/)).toBeInTheDocument();
    });
  });
});
