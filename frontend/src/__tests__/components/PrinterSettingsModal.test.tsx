import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { PrinterSettingsModal } from '../../components/PrinterSettingsModal';
import { api } from '../../api/client';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getPrinterSettings: vi.fn(),
    postPrinterSettings: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    updateSettings: vi.fn().mockResolvedValue({}),
    getCurrentUser: vi.fn().mockResolvedValue({
      id: 1, username: 'admin', role: 'admin',
      permissions: ['printers:read', 'printers:update'],
    }),
    getAuthStatus: vi.fn().mockResolvedValue({ setup_required: false, authenticated: true }),
  },
}));

const fullSupports = {
  spaghetti_detector: true, pileup_detector: true, nozzleclumping_detector: true,
  airprinting_detector: true, first_layer_inspector: true, ai_monitoring: true,
  filament_tangle: true, nozzle_blob: true, fod_check: true, displacement_detection: true,
  open_door_check: true, purify_air: false,
  auto_recovery: true, sound: true, save_remote_to_storage: true, snapshot: true,
  plate_type: false, plate_align: false,
  parts_editable: false, parts_dual: false,
};

const baseState = {
  auto_recovery: true, sound: false, filament_tangle: true, nozzle_blob: false,
  save_remote_to_storage: null, purify_air: null, open_door: 0,
  plate_type: null, plate_align: null, snapshot: false,
  fod_check: false, displacement_detection: false,
  spaghetti_detector: { enabled: true, sensitivity: 'medium' },
  pileup_detector: { enabled: false, sensitivity: 'medium' },
  nozzleclumping_detector: { enabled: false, sensitivity: 'medium' },
  airprinting_detector: { enabled: false, sensitivity: 'medium' },
  first_layer_inspector: { enabled: false, sensitivity: null },
  ai_monitoring: { enabled: false, sensitivity: null },
};

beforeEach(() => {
  vi.clearAllMocks();
  (api.getPrinterSettings as ReturnType<typeof vi.fn>).mockResolvedValue({
    print_options: baseState,
    parts: { nozzles: [{ id: 0, type: 'stainless_steel', diameter: 0.4, flow_type: 'standard' }] },
    supports: fullSupports,
  });
  (api.postPrinterSettings as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, sequence_id: 'S1' });
});

describe('PrinterSettingsModal', () => {
  it('returns null when closed', () => {
    render(<PrinterSettingsModal isOpen={false} onClose={() => {}} printerId={1} />);
    expect(screen.queryByText('Printer Settings')).toBeNull();
  });

  it('renders both tabs and switches between them', async () => {
    render(<PrinterSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    await waitFor(() => expect(screen.getByText('Print Options')).toBeInTheDocument());
    expect(screen.getByText('Printer Parts')).toBeInTheDocument();
    // Wait for query to resolve before tab content shows.
    await screen.findByText('Auto recovery from step loss');
    fireEvent.click(screen.getByText('Printer Parts'));
    expect(screen.getByText('Type')).toBeInTheDocument();
  });

  it('toggling Auto recovery sends print_option_bool', async () => {
    render(<PrinterSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    const cb = await screen.findByLabelText('Auto recovery from step loss');
    fireEvent.click(cb);
    await waitFor(() => {
      expect(api.postPrinterSettings).toHaveBeenCalledWith(1, {
        action: 'print_option_bool', key: 'auto_recovery', enabled: false,
      });
    });
  });

  it('hides unsupported rows', async () => {
    (api.getPrinterSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      print_options: baseState,
      parts: { nozzles: [] },
      supports: { ...fullSupports, spaghetti_detector: false, nozzle_blob: false },
    });
    render(<PrinterSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    await waitFor(() => expect(screen.getByText('Auto recovery from step loss')).toBeInTheDocument());
    expect(screen.queryByText('Spaghetti detection')).toBeNull();
    expect(screen.queryByText('Nozzle blob detection')).toBeNull();
  });
});
