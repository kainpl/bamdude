/**
 * A spool's own empty weight in the spool form (upstream b6521e93f).
 *
 * 0 g is a real tare: it used to load as 250 and be written back as 250 by any
 * edit. In Spoolman mode the tare is the spool's own `spool_weight`; it is sent
 * only when the operator changed it, so an unrelated save never stamps an
 * inherited figure onto the spool.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { SpoolFormModal } from '../../components/SpoolFormModal';
import type { InventorySpool } from '../../api/client';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getCurrentUser: vi.fn().mockResolvedValue({ id: 1, username: 'admin', is_admin: true, permissions: [] }),
    getSpoolFacets: vi.fn().mockResolvedValue({ categories: [] }),
    getFilamentFamilies: vi.fn().mockResolvedValue([]),
    triggerFilamentPresetSync: vi.fn().mockResolvedValue({ queued: true }),
    getSettings: vi.fn().mockResolvedValue({}),
    getUiPreferences: vi.fn().mockResolvedValue({}),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
    getCloudStatus: vi.fn().mockResolvedValue({ is_authenticated: false }),
    getFilamentPresets: vi.fn().mockResolvedValue([]),
    getSpoolCatalog: vi.fn().mockResolvedValue([]),
    getColorCatalog: vi.fn().mockResolvedValue([]),
    getLocalPresets: vi.fn().mockResolvedValue({ filament: [] }),
    getBuiltinFilaments: vi.fn().mockResolvedValue([]),
    getLocations: vi.fn().mockResolvedValue([]),
    getPrinters: vi.fn().mockResolvedValue([]),
    getSpoolUsageHistory: vi.fn().mockResolvedValue([]),
    getSpoolmanUsageHistory: vi.fn().mockResolvedValue([]),
    getSpoolmanFilaments: vi.fn().mockResolvedValue([]),
    getSpoolmanInventoryFilaments: vi.fn().mockResolvedValue([]),
    createSpool: vi.fn().mockResolvedValue({ id: 99 }),
    updateSpool: vi.fn().mockResolvedValue({ id: 1 }),
    createSpoolmanInventorySpool: vi.fn().mockResolvedValue({ id: 99 }),
    updateSpoolmanInventorySpool: vi.fn().mockResolvedValue({ id: 1 }),
    saveSpoolKProfiles: vi.fn().mockResolvedValue([]),
    saveSpoolmanKProfiles: vi.fn().mockResolvedValue([]),
  },
}));

vi.mock('../../components/spool-form/types', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../components/spool-form/types')>();
  return { ...actual, validateForm: vi.fn().mockReturnValue({ isValid: true, errors: {} }) };
});

vi.mock('../../contexts/ToastContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/ToastContext')>();
  return { ...actual, useToast: () => ({ showToast: vi.fn() }) };
});

import { api } from '../../api/client';

const spoolWith = (extra: Record<string, unknown>) =>
  ({
    id: 1,
    material: 'PLA',
    subtype: 'Basic',
    brand: 'Polymaker',
    color_name: 'Red',
    rgba: 'FF0000FF',
    label_weight: 1000,
    core_weight: 250,
    core_weight_catalog_id: null,
    weight_used: 300,
    slicer_filament: 'GFL99',
    slicer_filament_name: 'Generic PLA',
    filament_diameter: '1.75',
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    k_profiles: [],
    ...extra,
  }) as unknown as InventorySpool;

const open = (spool: InventorySpool, props: Partial<Parameters<typeof SpoolFormModal>[0]> = {}) =>
  render(<SpoolFormModal isOpen onClose={vi.fn()} spool={spool} currencySymbol="$" {...props} />);

const tareInput = async () => (await screen.findByLabelText('Empty Spool Weight')) as HTMLInputElement;

describe('SpoolFormModal — the spool\'s own empty weight', () => {
  beforeEach(() => vi.clearAllMocks());

  it('a 0 g tare loads as 0 and is saved as 0', async () => {
    open(spoolWith({ core_weight: 0 }));
    expect((await tareInput()).value).toBe('0');
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(api.updateSpool).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.updateSpool).mock.calls[0][1]).toHaveProperty('core_weight', 0);
  });

  it('Spoolman mode shows the tare and does not send it untouched', async () => {
    open(spoolWith({ core_weight: 180, core_weight_is_inherited: true }), { spoolmanMode: true });
    expect((await tareInput()).value).toBe('180');
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(api.updateSpoolmanInventorySpool).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.updateSpoolmanInventorySpool).mock.calls[0][1]).not.toHaveProperty('core_weight');
  });

  it('Spoolman mode sends a tare the operator changed — 0 included', async () => {
    open(spoolWith({ core_weight: 180, core_weight_is_inherited: true }), { spoolmanMode: true });
    fireEvent.change(await tareInput(), { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(api.updateSpoolmanInventorySpool).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.updateSpoolmanInventorySpool).mock.calls[0][1]).toHaveProperty('core_weight', 0);
  });

  it('a Spoolman copy carries the source\'s own tare, not an inherited one', async () => {
    const { unmount } = open(spoolWith({ core_weight: 199, core_weight_is_inherited: false }), {
      spoolmanMode: true,
      mode: 'copy',
    });
    await tareInput();
    fireEvent.click(screen.getByRole('button', { name: /copy spool/i }));
    await waitFor(() => expect(api.createSpoolmanInventorySpool).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.createSpoolmanInventorySpool).mock.calls[0][0]).toHaveProperty('core_weight', 199);
    unmount();
    vi.clearAllMocks();

    open(spoolWith({ core_weight: 199, core_weight_is_inherited: true }), { spoolmanMode: true, mode: 'copy' });
    await tareInput();
    fireEvent.click(screen.getByRole('button', { name: /copy spool/i }));
    await waitFor(() => expect(api.createSpoolmanInventorySpool).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.createSpoolmanInventorySpool).mock.calls[0][0]).not.toHaveProperty('core_weight');
  });
});
