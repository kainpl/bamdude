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
    orcaCloudStatus: vi.fn().mockResolvedValue({ connected: false }),
    getSlicerPresets: vi.fn(),
    getCloudSettingDetail: vi.fn().mockResolvedValue({ name: 'Cloud PETG', setting: {} }),
    getSettings: vi.fn().mockResolvedValue({}),
    // read by the providers the test render wraps the dialog in
    getUiPreferences: vi.fn().mockResolvedValue({}),
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
        push_to_orca: false,
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

  // "From an existing preset" on the slicing-setup family (spec §7.4).
  const fil = (source: string, id: string, name: string) => ({ id, name, source, filament_type: 'PETG' });
  const tiers = (over: Record<string, unknown[]>) => {
    const empty = { printer: [], process: [], filament: [] };
    return {
      local: { ...empty, filament: over.local ?? [] },
      cloud: { ...empty, filament: over.cloud ?? [] },
      orca_cloud: { ...empty, filament: over.orca_cloud ?? [] },
      standard: { ...empty, filament: over.standard ?? [] },
      cloud_status: 'ok',
      orca_cloud_status: 'ok',
    };
  };
  const fromPreset = () => fireEvent.click(screen.getByLabelText('From an existing preset'));

  it('filters the preset picker by slicer and keeps it searchable', async () => {
    vi.mocked(api.getSlicerPresets).mockResolvedValue(
      tiers({
        local: [fil('local', '7', 'My PETG')],
        cloud: [fil('cloud', 'PFUSx', 'Cloud PETG')],
        orca_cloud: [fil('orca_cloud', 'o1', 'Orca PETG')],
        standard: [fil('standard', 'Generic PETG', 'Generic PETG')],
      }) as never,
    );
    render(<CreateFilamentFamilyModal open onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('PETG')).toBeInTheDocument());
    fromPreset();
    const select = (await screen.findByLabelText('Preset to start from')) as HTMLSelectElement;
    await waitFor(() => expect(Array.from(select.options).map((o) => o.value)).toContain('local:7'));
    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toEqual(expect.arrayContaining(['local:7', 'cloud:PFUSx']));
    expect(values).not.toContain('orca_cloud:o1');
    expect(values).not.toContain('standard:Generic PETG');
    fireEvent.change(select, { target: { value: 'cloud:PFUSx' } });
    fireEvent.click(screen.getByRole('button', { name: 'OrcaSlicer' }));
    expect(Array.from(select.options).map((o) => o.value)).toContain('orca_cloud:o1');
    expect(Array.from(select.options).map((o) => o.value)).not.toContain('cloud:PFUSx');
    expect(select).toHaveValue('');
    expect(screen.getByText(/selected profile is from the other slicer/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'BambuStudio' }));
    expect(select).toHaveValue('cloud:PFUSx');
    expect(screen.getByRole('button', { name: /Search: Preset to start from/ })).toBeInTheDocument();
  });

  it('no catalogue request while closed or on From type', async () => {
    vi.mocked(api.getSlicerPresets).mockResolvedValue(tiers({ local: [fil('local', '7', 'My PETG')] }) as never);
    const closed = render(<CreateFilamentFamilyModal open={false} onClose={() => {}} />);
    await new Promise((r) => setTimeout(r, 20));
    closed.unmount();
    render(<CreateFilamentFamilyModal open onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('PETG')).toBeInTheDocument());
    await new Promise((r) => setTimeout(r, 20));
    expect(api.getSlicerPresets).not.toHaveBeenCalled();
    fromPreset();
    await waitFor(() => expect(api.getSlicerPresets).toHaveBeenCalled());
  });

  it('empty tiers: From existing is empty, From type stays available', async () => {
    vi.mocked(api.getSlicerPresets).mockResolvedValue(tiers({ standard: [fil('standard', 'Generic PETG', 'Generic PETG')] }) as never);
    render(<CreateFilamentFamilyModal open onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('PETG')).toBeInTheDocument());
    fromPreset();
    const select = (await screen.findByLabelText('Preset to start from')) as HTMLSelectElement;
    await waitFor(() => expect(select).toBeDisabled());
    expect(select.options[0].textContent).toMatch(/No presets available/);
    fireEvent.click(screen.getByLabelText('From type (standard profile)'));
    expect(screen.queryByLabelText('Preset to start from')).toBeNull();
  });

  it('a preset chosen there is sent as its source and id', async () => {
    vi.mocked(api.getSlicerPresets).mockResolvedValue(tiers({ local: [fil('local', '7', 'My PETG')] }) as never);
    render(<CreateFilamentFamilyModal open onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('PETG')).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Polymaker'), { target: { value: 'Poly' } });
    fireEvent.change(screen.getByPlaceholderText('Basic'), { target: { value: 'Basic' } });
    fromPreset();
    const select = (await screen.findByLabelText('Preset to start from')) as HTMLSelectElement;
    await waitFor(() => expect(Array.from(select.options).map((o) => o.value)).toContain('local:7'));
    fireEvent.change(select, { target: { value: 'local:7' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create filament' }));
    await waitFor(() =>
      expect(api.createFilamentFamily).toHaveBeenCalledWith(
        expect.objectContaining({ source_mode: 'preset', source: 'local', source_id: '7' }),
      ),
    );
  });
});
