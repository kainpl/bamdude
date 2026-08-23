/**
 * Tests for the CreateFilamentFamilyModal component (spec B): BS-parity
 * dialog data, printer PROFILES (not devices), client-side vendor refusals,
 * per-variant checkbox logic (local vs Bambu-tab).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { CreateFilamentFamilyModal } from '../../components/CreateFilamentFamilyModal';
import { api } from '../../api/client';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getFilamentAuthoringOptions: vi.fn().mockResolvedValue({
      filament_types: ['PLA', 'PETG', 'ABS'],
      push: { bambu: true, orca: false },
      printer_names: [
        'Bambu Lab P1S 0.4 nozzle',
        'Bambu Lab P1S 0.6 nozzle',
        'Bambu Lab X1 Carbon 0.4 nozzle',
      ],
    }),
    getPrinters: vi.fn().mockResolvedValue([{ id: 1, name: 'P1S left', model: 'P1S' }]),
    getPrinterModels: vi.fn().mockResolvedValue({ 'Bambu Lab P1S': 'P1S', 'Bambu Lab X1 Carbon': 'X1C' }),
    getCloudStatus: vi.fn().mockResolvedValue({ is_authenticated: false, email: null }),
    getSlicerPresets: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    updateSettings: vi.fn().mockResolvedValue({}),
    createFilamentFamily: vi.fn().mockResolvedValue({
      filament_id: 'Pabc1234',
      name: 'Poly PETG Basic',
      attached: false,
      roots: [],
      warnings: [],
      push: null,
    }),
  },
}));

describe('CreateFilamentFamilyModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('lists printer PROFILES and preselects the farm models', async () => {
    render(<CreateFilamentFamilyModal open onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('Bambu Lab X1 Carbon 0.4 nozzle')).toBeInTheDocument();
    });
    // Owned model (P1S) variants come pre-checked; the stranger does not.
    const p1s = screen.getByLabelText('Bambu Lab P1S 0.4 nozzle') as HTMLInputElement;
    const x1c = screen.getByLabelText('Bambu Lab X1 Carbon 0.4 nozzle') as HTMLInputElement;
    await waitFor(() => expect(p1s.checked).toBe(true));
    expect(x1c.checked).toBe(false);
  });

  it('refuses a reserved vendor client-side', async () => {
    render(<CreateFilamentFamilyModal open onClose={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText('Polymaker'), { target: { value: 'bambu' } });
    await waitFor(() => {
      expect(screen.getByText('This vendor name is reserved')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByPlaceholderText('Basic'), { target: { value: 'X' } });
    expect(screen.getByRole('button', { name: 'Create filament' })).toBeDisabled();
    expect(api.createFilamentFamily).not.toHaveBeenCalled();
  });

  it('submits printer profile names in the local variant', async () => {
    const onCreated = vi.fn();
    render(<CreateFilamentFamilyModal open onClose={() => {}} onCreated={onCreated} />);
    await waitFor(() => {
      expect(screen.getByText('PETG')).toBeInTheDocument();
    });
    const p1s = screen.getByLabelText('Bambu Lab P1S 0.4 nozzle') as HTMLInputElement;
    await waitFor(() => expect(p1s.checked).toBe(true));
    fireEvent.change(screen.getByPlaceholderText('Polymaker'), { target: { value: 'Poly' } });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'PETG' } });
    fireEvent.change(screen.getByPlaceholderText('Basic'), { target: { value: 'Basic' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create filament' }));
    await waitFor(() => {
      expect(api.createFilamentFamily).toHaveBeenCalledWith({
        vendor: 'Poly',
        filament_type: 'PETG',
        serial: 'Basic',
        printer_ids: [],
        printer_names: ['Bambu Lab P1S 0.4 nozzle', 'Bambu Lab P1S 0.6 nozzle'],
        source_mode: 'type',
        source: null,
        source_id: null,
        push_to_bambu: false,
        save_local: true,
      });
    });
    expect(onCreated).toHaveBeenCalledWith('Pabc1234', 'Poly PETG Basic');
  });

  it('bambu variant forces the push and offers "keep locally" instead', async () => {
    (api.getCloudStatus as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ is_authenticated: true, email: 'a@b.c' });
    render(<CreateFilamentFamilyModal open onClose={() => {}} variant="bambu" />);
    await waitFor(() => {
      expect(screen.getByText('Also keep locally')).toBeInTheDocument();
    });
    expect(screen.queryByText('Also push to Bambu Cloud')).not.toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('Polymaker'), { target: { value: 'Poly' } });
    fireEvent.change(screen.getByPlaceholderText('Basic'), { target: { value: 'Cl' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create filament' }));
    await waitFor(() => {
      expect(api.createFilamentFamily).toHaveBeenCalled();
    });
    const payload = (api.createFilamentFamily as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(payload.push_to_bambu).toBe(true);
    expect(payload.save_local).toBe(false);
  });

  it('bambu variant refuses to submit without a connected cloud', async () => {
    render(<CreateFilamentFamilyModal open onClose={() => {}} variant="bambu" />);
    await waitFor(() => {
      expect(screen.getByText('PETG')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByPlaceholderText('Polymaker'), { target: { value: 'Poly' } });
    fireEvent.change(screen.getByPlaceholderText('Basic'), { target: { value: 'X' } });
    expect(screen.getByRole('button', { name: 'Create filament' })).toBeDisabled();
  });

  it('hides the local-variant push checkbox when the cloud is not connected', async () => {
    render(<CreateFilamentFamilyModal open onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('PETG')).toBeInTheDocument();
    });
    expect(screen.queryByText('Also push to Bambu Cloud')).not.toBeInTheDocument();
  });
});
