/**
 * Tests for AMSSettingsModal (BS-port AMS Settings dialog).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { AMSSettingsModal } from '../../components/AMSSettingsModal';
import { api } from '../../api/client';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getAmsSettings: vi.fn(),
    postAmsSettings: vi.fn(),
    // ThemeContext + AuthContext run on mount via the wrapper providers.
    getSettings: vi.fn().mockResolvedValue({}),
    updateSettings: vi.fn().mockResolvedValue({}),
    getCurrentUser: vi.fn().mockResolvedValue({
      id: 1,
      username: 'admin',
      role: 'admin',
      permissions: ['printers:read', 'printers:update'],
    }),
    getAuthStatus: vi.fn().mockResolvedValue({ setup_required: false, authenticated: true }),
  },
}));

const baseSupports = {
  insertion_update: true,
  power_on_update: true,
  remain_capacity: true,
  auto_switch_filament: true,
  air_print_detect: false,
  firmware_switch: false,
  reorder: false,
};

const baseState = {
  insertion_update: true,
  power_on_update: false,
  remain_capacity: true,
  auto_switch_filament: true,
  air_print_detect: null,
  firmware_idx_run: null,
  firmware_idx_sel: null,
  firmware_switching: false,
};

beforeEach(() => {
  vi.clearAllMocks();
  (api.getAmsSettings as ReturnType<typeof vi.fn>).mockResolvedValue({
    state: baseState,
    supports: baseSupports,
    ams_units: [{ ams_id: 0, label: 'AMS A' }],
    firmware_options: [],
  });
  (api.postAmsSettings as ReturnType<typeof vi.fn>).mockResolvedValue({
    ok: true,
    sequence_id: 'S1',
  });
});

describe('AMSSettingsModal', () => {
  it('renders nothing when closed', () => {
    render(<AMSSettingsModal isOpen={false} onClose={() => {}} printerId={1} />);
    expect(screen.queryByText('AMS Settings')).toBeNull();
  });

  it('shows skeleton then supported rows; hides unsupported air_print row', async () => {
    render(<AMSSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    await waitFor(() => expect(screen.getByText('Insertion update')).toBeInTheDocument());
    expect(screen.getByText('Power on update')).toBeInTheDocument();
    expect(screen.getByText('Update remaining capacity')).toBeInTheDocument();
    expect(screen.getByText('AMS filament backup')).toBeInTheDocument();
    expect(screen.queryByText('Air Printing Detection')).toBeNull();
  });

  it('toggling Insertion update sends user_setting with current values for the other two', async () => {
    render(<AMSSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    const cb = await screen.findByLabelText('Insertion update');
    fireEvent.click(cb);
    await waitFor(() => {
      expect(api.postAmsSettings).toHaveBeenCalledWith(1, {
        action: 'user_setting',
        tray_read_option: false,        // was true, toggled OFF
        startup_read_option: false,     // mirrors baseState.power_on_update
        calibrate_remain_flag: true,    // mirrors baseState.remain_capacity
      });
    });
  });

  it('reverts on API error', async () => {
    (api.postAmsSettings as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('boom'));
    render(<AMSSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    const cb = await screen.findByLabelText('Insertion update') as HTMLInputElement;
    expect(cb.checked).toBe(true);
    fireEvent.click(cb);
    // After failure local state reverts to original True.
    await waitFor(() => expect(cb.checked).toBe(true));
  });

  it('reorder button opens confirm dialog and sends reorder action with no payload', async () => {
    (api.getAmsSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      state: baseState,
      supports: { ...baseSupports, reorder: true },
      ams_units: [{ ams_id: 0, label: 'AMS A' }],
      firmware_options: [],
    });
    render(<AMSSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);
    const resetBtn = await screen.findByText('Reset');
    fireEvent.click(resetBtn);
    const confirm = await screen.findByText('Confirm');
    fireEvent.click(confirm);
    await waitFor(() => {
      expect(api.postAmsSettings).toHaveBeenCalledWith(1, { action: 'reorder' });
    });
  });

  // ---- AMS firmware switch -------------------------------------------------
  // The picker used to carry hardcoded FULL/LITE labels against ids 0/1, while
  // BS's enum is IDX_LITE = 0 — so every click sent the opposite personality.
  // The labels now come from the device, which is what makes that class of bug
  // impossible rather than merely fixed.

  const fwResponse = (overrides: Record<string, unknown> = {}) => ({
    state: { ...baseState, firmware_idx_sel: 1, firmware_idx_run: 1, ...overrides },
    supports: { ...baseSupports, firmware_switch: true },
    ams_units: [{ ams_id: 0, label: 'AMS A' }],
    firmware_options: [
      { idx: 0, label: 'AMS Lite', version: '00.00.06.15' },
      { idx: 1, label: 'AMS / AMS2 / AMS HT', version: '00.00.07.89' },
    ],
  });

  it('labels the firmware options with the names the device reported', async () => {
    (api.getAmsSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce(fwResponse());
    render(<AMSSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);

    const lite = await screen.findByRole('option', { name: /AMS Lite/ });
    expect((lite as HTMLOptionElement).value).toBe('0');
    const full = screen.getByRole('option', { name: /AMS \/ AMS2 \/ AMS HT/ });
    expect((full as HTMLOptionElement).value).toBe('1');
  });

  it('hides the picker while a switch is in progress', async () => {
    (api.getAmsSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      fwResponse({ firmware_switching: true }),
    );
    render(<AMSSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);

    await screen.findByText(/Switching AMS type/);
    expect(screen.queryByRole('option', { name: /AMS Lite/ })).toBeNull();
  });

  it('does not pre-select a personality the printer never reported', async () => {
    // The old code fell back to `?? 0`, and 0 is a real id (IDX_LITE) — so an
    // unreported state rendered as "Lite is selected".
    (api.getAmsSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      fwResponse({ firmware_idx_sel: null, firmware_idx_run: null }),
    );
    render(<AMSSettingsModal isOpen={true} onClose={() => {}} printerId={1} />);

    const select = (await screen.findByRole('option', { name: /AMS Lite/ }))
      .closest('select') as HTMLSelectElement;
    expect(select.value).toBe('');
  });
});
