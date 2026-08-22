/**
 * Tests for the CreateFilamentFamilyModal component (spec B): BS-parity
 * dialog data, client-side vendor refusals, submit payload, push gating.
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
    }),
    getPrinters: vi.fn().mockResolvedValue([
      { id: 1, name: 'P1S left' },
      { id: 2, name: 'X1C right' },
    ]),
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

  it('renders type options from authoring-options', async () => {
    render(<CreateFilamentFamilyModal open onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('PETG')).toBeInTheDocument();
    });
    expect(api.getFilamentAuthoringOptions).toHaveBeenCalled();
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

  it('submits the create payload and reports the new family', async () => {
    const onCreated = vi.fn();
    render(<CreateFilamentFamilyModal open onClose={() => {}} onCreated={onCreated} />);
    await waitFor(() => {
      expect(screen.getByText('PETG')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByPlaceholderText('Polymaker'), { target: { value: 'Poly' } });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'PETG' } });
    fireEvent.change(screen.getByPlaceholderText('Basic'), { target: { value: 'Basic' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create filament' }));
    await waitFor(() => {
      expect(api.createFilamentFamily).toHaveBeenCalledWith({
        vendor: 'Poly',
        filament_type: 'PETG',
        serial: 'Basic',
        printer_ids: [1, 2],
        source_mode: 'type',
        source: null,
        source_id: null,
        push_to_bambu: false,
      });
    });
    expect(onCreated).toHaveBeenCalledWith('Pabc1234', 'Poly PETG Basic');
  });

  it('hides the push checkbox when the cloud is not connected', async () => {
    render(<CreateFilamentFamilyModal open onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('PETG')).toBeInTheDocument();
    });
    expect(screen.queryByText('Also push to Bambu Cloud')).not.toBeInTheDocument();
  });
});
