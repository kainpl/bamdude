/**
 * Tests for the SpoolmanSettings component.
 *
 * Tests the filament tracking mode selector and Spoolman integration UI:
 * - Mode selector (Built-in Inventory vs Spoolman)
 * - Built-in Inventory info panel
 * - Spoolman URL, sync mode, connection status
 * - Weight sync and partial usage toggles
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { SpoolmanSettings } from '../../components/SpoolmanSettings';

// Mock the API client
vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getSettings: vi.fn().mockResolvedValue({}),
    updateSettings: vi.fn().mockResolvedValue({}),
    getSpoolmanSettings: vi.fn(),
    updateSpoolmanSettings: vi.fn(),
    getSpoolmanStatus: vi.fn(),
    connectSpoolman: vi.fn(),
    disconnectSpoolman: vi.fn(),
    syncAllPrintersAms: vi.fn(),
    syncPrinterAms: vi.fn(),
    getPrinters: vi.fn(),
    getSpoolmanModeSwitchPreview: vi.fn(),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
  },
}));

// Import mocked module
import { api } from '../../api/client';

describe('SpoolmanSettings', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    // Default API mocks - Spoolman disabled (Built-in Inventory mode)
    vi.mocked(api.getSpoolmanSettings).mockResolvedValue({
      spoolman_enabled: 'false',
      spoolman_url: '',
      spoolman_sync_mode: 'auto',
      spoolman_disable_weight_sync: 'false',
      spoolman_report_partial_usage: 'true',
      auto_add_unknown_rfid: 'false',
    });
    vi.mocked(api.updateSpoolmanSettings).mockResolvedValue({
      spoolman_enabled: 'false',
      spoolman_url: '',
      spoolman_sync_mode: 'auto',
      spoolman_disable_weight_sync: 'false',
      spoolman_report_partial_usage: 'true',
      auto_add_unknown_rfid: 'false',
    });
    vi.mocked(api.getSpoolmanStatus).mockResolvedValue({
      enabled: false,
      connected: false,
      url: null,
    });
    vi.mocked(api.getPrinters).mockResolvedValue([]);
    vi.mocked(api.getSpoolmanModeSwitchPreview).mockResolvedValue({ assignments: 3, printing: [] });
    vi.mocked(api.connectSpoolman).mockResolvedValue({ success: true, message: 'Connected' });
    vi.mocked(api.disconnectSpoolman).mockResolvedValue({ success: true, message: 'Disconnected' });
    vi.mocked(api.syncAllPrintersAms).mockResolvedValue({
      success: true,
      synced_count: 3,
      skipped_count: 1,
      skipped: [],
      errors: [],
    });
  });

  describe('rendering', () => {
    it('renders loading state initially', () => {
      vi.mocked(api.getSpoolmanSettings).mockImplementation(() => new Promise(() => {}));
      render(<SpoolmanSettings />);

      expect(document.querySelector('.animate-spin')).toBeInTheDocument();
    });

    it('renders filament tracking title', async () => {
      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Filament Tracking')).toBeInTheDocument();
      });
    });

    it('renders mode selector cards', async () => {
      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Built-in Inventory')).toBeInTheDocument();
        expect(screen.getByText('Spoolman')).toBeInTheDocument();
      });
    });
  });

  describe('built-in inventory mode (default)', () => {
    it('shows built-in inventory as selected by default', async () => {
      render(<SpoolmanSettings />);

      await waitFor(() => {
        // Built-in Inventory card should have the active border
        const builtInBtn = screen.getByText('Built-in Inventory').closest('button');
        expect(builtInBtn).toHaveClass('border-bambu-green');
      });
    });

    it('shows built-in info panel when selected', async () => {
      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText(/Automatically detects Bambu Lab RFID spools/)).toBeInTheDocument();
      });
    });

    it('does not show Spoolman URL input', async () => {
      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Filament Tracking')).toBeInTheDocument();
      });

      expect(screen.queryByPlaceholderText('http://192.168.1.100:7912')).not.toBeInTheDocument();
    });
  });

  describe('spoolman mode', () => {
    beforeEach(() => {
      vi.mocked(api.getSpoolmanSettings).mockResolvedValue({
        spoolman_enabled: 'true',
        spoolman_url: 'http://localhost:7912',
        spoolman_sync_mode: 'auto',
        spoolman_disable_weight_sync: 'false',
        spoolman_report_partial_usage: 'true',
        auto_add_unknown_rfid: 'false',
      });
      vi.mocked(api.updateSpoolmanSettings).mockResolvedValue({
        spoolman_enabled: 'true',
        spoolman_url: 'http://localhost:7912',
        spoolman_sync_mode: 'auto',
        spoolman_disable_weight_sync: 'false',
        spoolman_report_partial_usage: 'true',
        auto_add_unknown_rfid: 'false',
      });
    });

    it('shows Spoolman card as selected', async () => {
      render(<SpoolmanSettings />);

      await waitFor(() => {
        const spoolmanBtn = screen.getByText('Spoolman').closest('button');
        expect(spoolmanBtn).toHaveClass('border-bambu-green');
      });
    });

    it('shows URL input when Spoolman is selected', async () => {
      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByPlaceholderText('http://192.168.1.100:7912')).toBeInTheDocument();
      });
    });

    it('shows sync mode selector', async () => {
      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Sync Mode')).toBeInTheDocument();
      });
    });

    it('shows how sync works info', async () => {
      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('How Sync Works')).toBeInTheDocument();
      });
    });

    it('shows connection status section', async () => {
      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Status:')).toBeInTheDocument();
      });
    });

    it('shows Disconnected when not connected', async () => {
      vi.mocked(api.getSpoolmanStatus).mockResolvedValue({
        enabled: true,
        connected: false,
        url: 'http://localhost:7912',
      });

      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Disconnected')).toBeInTheDocument();
      });
    });

    it('shows Connected and offers nothing to press when connected', async () => {
      // Spoolman is a stateless HTTP API with no session to close, so there is
      // nothing for a Disconnect button to disconnect: it dropped this
      // process's client object, which the next request rebuilt lazily, and the
      // status flipped back on its own (upstream 4e40a502). Turning the
      // integration off is the enable toggle's job.
      vi.mocked(api.getSpoolmanStatus).mockResolvedValue({
        enabled: true,
        connected: true,
        url: 'http://localhost:7912',
      });

      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Connected')).toBeInTheDocument();
      });
      expect(screen.queryByText('Disconnect')).not.toBeInTheDocument();
      expect(screen.queryByText('Connect')).not.toBeInTheDocument();
    });

    it('offers Connect as a retry only while Spoolman is unreachable', async () => {
      vi.mocked(api.getSpoolmanStatus).mockResolvedValue({
        enabled: true,
        connected: false,
        url: 'http://localhost:7912',
      });

      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Connect')).toBeInTheDocument();
      });
      expect(screen.queryByText('Disconnect')).not.toBeInTheDocument();
    });

    it('shows sync section when connected', async () => {
      vi.mocked(api.getSpoolmanStatus).mockResolvedValue({
        enabled: true,
        connected: true,
        url: 'http://localhost:7912',
      });

      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Sync AMS Data')).toBeInTheDocument();
      });
    });
  });

  describe('weight sync toggle', () => {
    it('shows weight sync toggle when Spoolman enabled and sync mode is auto', async () => {
      vi.mocked(api.getSpoolmanSettings).mockResolvedValue({
        spoolman_enabled: 'true',
        spoolman_url: 'http://localhost:7912',
        spoolman_sync_mode: 'auto',
        spoolman_disable_weight_sync: 'false',
        spoolman_report_partial_usage: 'true',
        auto_add_unknown_rfid: 'false',
      });

      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Disable AMS Estimated Weight Sync')).toBeInTheDocument();
      });
    });

    it('does not show weight sync toggle when sync mode is manual', async () => {
      vi.mocked(api.getSpoolmanSettings).mockResolvedValue({
        spoolman_enabled: 'true',
        spoolman_url: 'http://localhost:7912',
        spoolman_sync_mode: 'manual',
        spoolman_disable_weight_sync: 'false',
        spoolman_report_partial_usage: 'true',
        auto_add_unknown_rfid: 'false',
      });

      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Filament Tracking')).toBeInTheDocument();
      });

      expect(screen.queryByText('Disable AMS Estimated Weight Sync')).not.toBeInTheDocument();
    });
  });

  describe('partial usage toggle', () => {
    it('shows partial usage toggle when weight sync is disabled', async () => {
      vi.mocked(api.getSpoolmanSettings).mockResolvedValue({
        spoolman_enabled: 'true',
        spoolman_url: 'http://localhost:7912',
        spoolman_sync_mode: 'auto',
        spoolman_disable_weight_sync: 'true',
        spoolman_report_partial_usage: 'true',
        auto_add_unknown_rfid: 'false',
      });

      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Report Partial Usage for Failed Prints')).toBeInTheDocument();
      });
    });

    it('does not show partial usage toggle when weight sync is enabled', async () => {
      vi.mocked(api.getSpoolmanSettings).mockResolvedValue({
        spoolman_enabled: 'true',
        spoolman_url: 'http://localhost:7912',
        spoolman_sync_mode: 'auto',
        spoolman_disable_weight_sync: 'false',
        spoolman_report_partial_usage: 'true',
        auto_add_unknown_rfid: 'false',
      });

      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Filament Tracking')).toBeInTheDocument();
      });

      expect(screen.queryByText('Report Partial Usage for Failed Prints')).not.toBeInTheDocument();
    });
  });

  describe('mode switching', () => {
    // Switching clears every slot assignment of the mode being left, so it is
    // asked first and saved only on the answer (audit D4, upstream #2812: the
    // page used to save the click by itself 500 ms later, and four clicks of
    // someone looking wiped the configuration).

    it('can switch to Spoolman mode once confirmed', async () => {
      const user = userEvent.setup();
      render(<SpoolmanSettings />);

      await waitFor(() => {
        expect(screen.getByText('Built-in Inventory')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Spoolman').closest('button')!);
      await user.click(await screen.findByRole('button', { name: 'Switch' }));

      await waitFor(() => {
        expect(api.updateSpoolmanSettings).toHaveBeenCalledWith({ spoolman_enabled: 'true' });
      });
      await waitFor(() => {
        expect(screen.getByPlaceholderText('http://192.168.1.100:7912')).toBeInTheDocument();
      });
    });

    it('asks first, naming what goes, and saves nothing on the click alone', async () => {
      const user = userEvent.setup();
      render(<SpoolmanSettings />);
      await screen.findByText('Built-in Inventory');

      await user.click(screen.getByText('Spoolman').closest('button')!);

      expect(await screen.findByText('Switch filament tracking?')).toBeInTheDocument();
      expect(await screen.findByText(/3 slot assignments will be removed/)).toBeInTheDocument();
      expect(api.getSpoolmanModeSwitchPreview).toHaveBeenCalledWith(true);
      await new Promise((resolve) => setTimeout(resolve, 700)); // past the autosave debounce
      expect(api.updateSpoolmanSettings).not.toHaveBeenCalled();
    });

    it('cancel leaves the mode as it was', async () => {
      const user = userEvent.setup();
      render(<SpoolmanSettings />);
      await screen.findByText('Built-in Inventory');

      await user.click(screen.getByText('Spoolman').closest('button')!);
      await user.click(await screen.findByRole('button', { name: 'Cancel' }));

      await new Promise((resolve) => setTimeout(resolve, 700));
      expect(api.updateSpoolmanSettings).not.toHaveBeenCalled();
      expect(screen.getByText('Built-in Inventory').closest('button')).toHaveClass('border-bambu-green');
    });

    it('names the printers printing right now', async () => {
      vi.mocked(api.getSpoolmanModeSwitchPreview).mockResolvedValue({ assignments: 0, printing: ['X1C-1', 'P1S-2'] });
      const user = userEvent.setup();
      render(<SpoolmanSettings />);
      await screen.findByText('Built-in Inventory');

      await user.click(screen.getByText('Spoolman').closest('button')!);

      expect(await screen.findByText(/X1C-1, P1S-2/)).toBeInTheDocument();
    });

    it('clicking the mode already on asks nothing', async () => {
      const user = userEvent.setup();
      render(<SpoolmanSettings />);
      await screen.findByText('Built-in Inventory');

      await user.click(screen.getByText('Built-in Inventory').closest('button')!);

      expect(screen.queryByText('Switch filament tracking?')).not.toBeInTheDocument();
      expect(api.getSpoolmanModeSwitchPreview).not.toHaveBeenCalled();
    });
  });
});
