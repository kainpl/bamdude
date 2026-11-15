/**
 * The material number — an internal article code on a spool (upstream 71d4b2f70).
 * The spool form's field, the operator's spool-order level, the naming
 * placeholder and the statistics breakdown.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { SpoolFormModal } from '../../components/SpoolFormModal';
import { MaterialNumberBreakdown } from '../../components/MaterialNumberBreakdown';
import type { InventorySpool } from '../../api/client';
import { compareByOrder, parseSpoolSortOrder, SPOOL_SORT_FIELDS } from '../../utils/spoolSort';
import { formatSpoolDisplayName } from '../../utils/spoolName';
import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getCurrentUser: vi.fn().mockResolvedValue({ id: 1, username: 'admin', is_admin: true, permissions: [] }),
    getSpoolFacets: vi.fn().mockResolvedValue({ categories: [], material_numbers: ['15', 'A-7'] }),
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

const spool = (extra: Record<string, unknown> = {}) =>
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

describe('the spool form', () => {
  beforeEach(() => vi.clearAllMocks());

  it('loads the number and saves it trimmed', async () => {
    render(<SpoolFormModal isOpen onClose={vi.fn()} spool={spool({ material_number: '15' })} currencySymbol="$" />);
    const input = (await screen.findByLabelText('Material number')) as HTMLInputElement;
    expect(input.value).toBe('15');

    fireEvent.change(input, { target: { value: '  A-7 ' } });
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(api.updateSpool).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.updateSpool).mock.calls[0][1]).toHaveProperty('material_number', 'A-7');
  });

  it('an emptied field is sent as no number', async () => {
    render(<SpoolFormModal isOpen onClose={vi.fn()} spool={spool({ material_number: '15' })} currencySymbol="$" />);
    fireEvent.change(await screen.findByLabelText('Material number'), { target: { value: ' ' } });
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(api.updateSpool).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.updateSpool).mock.calls[0][1]).toHaveProperty('material_number', null);
  });

  it('Spoolman mode shows the article number of the filament read-only and never sends it', async () => {
    render(
      <SpoolFormModal isOpen onClose={vi.fn()} spool={spool({ material_number: '15' })} currencySymbol="$" spoolmanMode />,
    );
    const input = (await screen.findByLabelText('Material number')) as HTMLInputElement;
    expect(input.value).toBe('15');
    expect(input.disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(api.updateSpoolmanInventorySpool).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.updateSpoolmanInventorySpool).mock.calls[0][1]).not.toHaveProperty('material_number');
  });
});

describe('the spool order and the naming template', () => {
  it('material number is a level, with a label in both languages', () => {
    expect(SPOOL_SORT_FIELDS).toContain('material_number');
    expect(parseSpoolSortOrder('material_number_desc')).toEqual([{ field: 'material_number', direction: 'desc' }]);
    const sorted = [
      spool({ id: 1, material_number: 'b' }),
      spool({ id: 2, material_number: null }),
      spool({ id: 3, material_number: 'A' }),
    ].sort(compareByOrder(parseSpoolSortOrder('material_number_asc')));
    expect(sorted.map((s) => s.id)).toEqual([2, 3, 1]);
    expect(en.settings.spoolSortOrder.fields.material_number).toBe('Material number');
    expect(uk.settings.spoolSortOrder.fields.material_number).toBe('Номер матеріалу');
  });

  it('{material_number} is a naming placeholder', () => {
    expect(formatSpoolDisplayName(spool({ material_number: '15' }), '[{material_number}] {brand}')).toBe(
      '[15] Polymaker',
    );
  });
});

describe('MaterialNumberBreakdown', () => {
  it('lists consumption and cost per number', () => {
    render(
      <MaterialNumberBreakdown
        currency="€"
        rows={[
          { material_number: '15', grams: 1500, cost: 30.5, spools: 2 },
          { material_number: 'A-7', grams: 30, cost: 0, spools: 1 },
        ]}
      />,
    );
    expect(screen.getByText('By material number')).toBeInTheDocument();
    expect(screen.getByText('15')).toBeInTheDocument();
    expect(screen.getByText('€30.50')).toBeInTheDocument();
    expect(screen.getByText('A-7')).toBeInTheDocument();
  });

  it('renders nothing when no spool with a number was used', () => {
    const { container } = render(<MaterialNumberBreakdown currency="€" rows={[]} />);
    expect(container.querySelector('table')).toBeNull();
    expect(screen.queryByText('By material number')).toBeNull();
  });
});
