/**
 * BulkEditSpoolsModal — only the fields the user explicitly enables are sent,
 * and usage fields are never included. Shared values pre-fill; differing values
 * show "— varies —".
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { BulkEditSpoolsModal } from '../../components/BulkEditSpoolsModal';
import type { InventorySpool } from '../../api/client';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getSettings: vi.fn().mockResolvedValue({}),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
    getFilamentPresets: vi.fn().mockResolvedValue([]),
    getLocalPresets: vi.fn().mockResolvedValue({ filament: [] }),
    getBuiltinFilaments: vi.fn().mockResolvedValue([]),
    getColorCatalog: vi.fn().mockResolvedValue([]),
    bulkUpdateSpools: vi.fn().mockResolvedValue([{ id: 1 }, { id: 2 }]),
  },
}));

import { api } from '../../api/client';

const spools = [
  { id: 1, material: 'PLA', brand: 'Acme', color_name: 'Red', rgba: 'FF0000FF', label_weight: 1000, weight_used: 100 },
  { id: 2, material: 'PLA', brand: 'Acme', color_name: 'Blue', rgba: '0000FFFF', label_weight: 1000, weight_used: 250 },
] as unknown as InventorySpool[];

describe('BulkEditSpoolsModal', () => {
  beforeEach(() => vi.clearAllMocks());

  it('sends only enabled fields and never usage', async () => {
    render(<BulkEditSpoolsModal isOpen spools={spools} allSpools={spools} catalogEntries={[]} onClose={vi.fn()} onSaved={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('Bulk edit spools')).toBeInTheDocument());

    // Enable the Brand field (shared value 'Acme' pre-fills), then change it.
    fireEvent.click(screen.getByLabelText('Brand'));
    const brandInput = await screen.findByDisplayValue('Acme');
    fireEvent.change(brandInput, { target: { value: 'NewCo' } });

    fireEvent.click(screen.getByText('Apply to 2'));

    await waitFor(() => expect(api.bulkUpdateSpools).toHaveBeenCalledTimes(1));
    const [ids, fields] = (api.bulkUpdateSpools as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(ids).toEqual([1, 2]);
    expect(fields).toEqual({ brand: 'NewCo' }); // only the enabled field; no weight_used
  });

  it('shows "— varies —" for fields that differ across the selection', async () => {
    render(<BulkEditSpoolsModal isOpen spools={spools} allSpools={spools} catalogEntries={[]} onClose={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Bulk edit spools')).toBeInTheDocument());
    // color_name differs (Red vs Blue) → its input placeholder is the "varies" marker.
    expect(screen.getAllByPlaceholderText('— varies —').length).toBeGreaterThan(0);
  });
});
