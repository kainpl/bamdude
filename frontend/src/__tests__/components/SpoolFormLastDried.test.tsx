/**
 * «Last dried» in the spool form (upstream 84f320b85).
 *
 * The date is stamped by an AMS drying cycle and can be set by hand for a
 * spool dried elsewhere. It is sent only when the operator touched it — an
 * unrelated save must not put back a date a cycle stamped while the form was
 * open — and a new or copied spool has no drying record at all.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { SpoolFormModal } from '../../components/SpoolFormModal';
import type { InventorySpool } from '../../api/client';
import { parseUTCDate, toDateTimeLocalValue } from '../../utils/date';

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
    createSpool: vi.fn().mockResolvedValue({ id: 99 }),
    updateSpool: vi.fn().mockResolvedValue({ id: 1 }),
    saveSpoolKProfiles: vi.fn().mockResolvedValue([]),
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

const STAMPED = '2026-10-06T11:05:00';

const spool = {
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
  last_dried_at: STAMPED,
  last_dried_temp: 55,
  last_dried_hours: 8,
} as unknown as InventorySpool;

const open = (props: Partial<Parameters<typeof SpoolFormModal>[0]> = {}) =>
  render(<SpoolFormModal isOpen onClose={vi.fn()} spool={spool} currencySymbol="$" {...props} />);

const save = async () => {
  fireEvent.click(screen.getByRole('button', { name: /save/i }));
  await waitFor(() => expect(api.updateSpool).toHaveBeenCalledTimes(1));
  return vi.mocked(api.updateSpool).mock.calls[0][1] as Record<string, unknown>;
};

describe('SpoolFormModal — last dried', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the stored moment in the browser\'s own time and explains where it comes from', async () => {
    open();
    const input = await screen.findByLabelText('Last dried') as HTMLInputElement;
    expect(input.type).toBe('datetime-local');
    expect(input.value).toBe(toDateTimeLocalValue(parseUTCDate(STAMPED)!));
    expect(screen.getByText(/ran for at least half its time/)).toBeInTheDocument();
  });

  it('does not send the date when it was not touched', async () => {
    open();
    await screen.findByLabelText('Last dried');
    expect(await save()).not.toHaveProperty('last_dried_at');
  });

  it('«Now» sends this moment', async () => {
    open();
    await screen.findByLabelText('Last dried');
    const before = Date.now();
    fireEvent.click(screen.getByRole('button', { name: 'Now' }));
    const payload = await save();
    const sent = new Date(payload.last_dried_at as string).getTime();
    // datetime-local carries minutes: the value is this minute, seconds dropped.
    expect(sent).toBeGreaterThan(before - 61_000);
    expect(sent).toBeLessThanOrEqual(Date.now());
    expect(payload.last_dried_at).toMatch(/Z$/);
  });

  it('a typed date is sent as the same moment in UTC', async () => {
    open();
    const input = await screen.findByLabelText('Last dried');
    fireEvent.change(input, { target: { value: '2026-03-04T05:06' } });
    const payload = await save();
    expect(payload.last_dried_at).toBe(new Date('2026-03-04T05:06').toISOString());
  });

  it('an emptied field clears the date', async () => {
    open();
    const input = await screen.findByLabelText('Last dried');
    fireEvent.change(input, { target: { value: '' } });
    expect(await save()).toHaveProperty('last_dried_at', null);
  });

  it('a new spool has no such field', async () => {
    render(<SpoolFormModal isOpen onClose={vi.fn()} currencySymbol="$" />);
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Add Spool' })).toBeInTheDocument());
    expect(screen.queryByLabelText('Last dried')).toBeNull();
  });

  it('a copy does not carry the drying record', async () => {
    open({ mode: 'copy' });
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Copy Spool' })).toBeInTheDocument());
    expect(screen.queryByLabelText('Last dried')).toBeNull();
    const saveBtn = screen.getAllByRole('button', { name: /copy spool/i })
      .find((btn) => btn.tagName === 'BUTTON' && btn.querySelector('svg'));
    fireEvent.click(saveBtn!);
    await waitFor(() => expect(api.createSpool).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.createSpool).mock.calls[0][0]).not.toHaveProperty('last_dried_at');
  });
});
