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
    getUiPreferences: vi.fn().mockResolvedValue({}),
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
    render(<BulkEditSpoolsModal isOpen spools={spools} allSpools={spools} catalogEntries={[]} spoolDisplayTemplate="{brand} {material} {color_name}" onClose={vi.fn()} onSaved={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('Bulk edit spools')).toBeInTheDocument());

    // Enable the Brand field. It's an autocomplete combobox that starts empty
    // (shared 'Acme' shown as placeholder, not pre-filled) so the dropdown can
    // offer other brands; type a new value.
    fireEvent.click(screen.getByLabelText('Brand'));
    const brandInput = screen.getByPlaceholderText('Acme');
    fireEvent.change(brandInput, { target: { value: 'NewCo' } });

    fireEvent.click(screen.getByText('Apply to 2'));

    await waitFor(() => expect(api.bulkUpdateSpools).toHaveBeenCalledTimes(1));
    const [ids, fields] = (api.bulkUpdateSpools as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(ids).toEqual([1, 2]);
    expect(fields).toEqual({ brand: 'NewCo' }); // only the enabled field; no weight_used
  });

  it('marks the selection as dried now, the time taken when it is applied', async () => {
    render(<BulkEditSpoolsModal isOpen spools={spools} allSpools={spools} catalogEntries={[]} spoolDisplayTemplate="{brand} {material} {color_name}" onClose={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Bulk edit spools')).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText('Last dried'));
    const before = Date.now();
    fireEvent.click(screen.getByText('Apply to 2'));

    await waitFor(() => expect(api.bulkUpdateSpools).toHaveBeenCalledTimes(1));
    const [ids, fields] = (api.bulkUpdateSpools as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(ids).toEqual([1, 2]);
    expect(Object.keys(fields)).toEqual(['last_dried_at']);
    expect(fields.last_dried_at).toMatch(/Z$/);
    const sent = new Date(fields.last_dried_at).getTime();
    expect(sent).toBeGreaterThanOrEqual(before - 1000);
    expect(sent).toBeLessThanOrEqual(Date.now() + 1000);
  });

  it('clears the drying date of the selection', async () => {
    render(<BulkEditSpoolsModal isOpen spools={spools} allSpools={spools} catalogEntries={[]} spoolDisplayTemplate="{brand} {material} {color_name}" onClose={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Bulk edit spools')).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText('Last dried'));
    fireEvent.change(screen.getByLabelText('Last dried: what to set'), { target: { value: 'clear' } });
    fireEvent.click(screen.getByText('Apply to 2'));

    await waitFor(() => expect(api.bulkUpdateSpools).toHaveBeenCalledTimes(1));
    const [, fields] = (api.bulkUpdateSpools as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(fields).toEqual({ last_dried_at: null });
  });

  it('leaves the drying date alone unless its box is ticked, and keeps the list read-only', async () => {
    render(<BulkEditSpoolsModal isOpen spools={spools} allSpools={spools} catalogEntries={[]} spoolDisplayTemplate="{brand} {material} {color_name}" onClose={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Bulk edit spools')).toBeInTheDocument());

    expect(screen.getByLabelText('Last dried: what to set')).toBeDisabled();
    // One checkbox per editable field and none per spool: the list of what will change is read-only.
    expect(screen.getByText('2 spools will change')).toBeInTheDocument();
    const boxes = screen.getAllByRole('checkbox');
    expect(boxes.every((box) => box.getAttribute('aria-label'))).toBe(true);
    expect(boxes.some((box) => /Acme PLA/.test(box.getAttribute('aria-label') ?? ''))).toBe(false);
  });

  it('unlocks the weight of the selection, and sends no weight', async () => {
    const locked = [{ ...spools[0], weight_locked: true }, spools[1]] as unknown as InventorySpool[];
    render(<BulkEditSpoolsModal isOpen spools={locked} allSpools={locked} catalogEntries={[]} spoolDisplayTemplate="{brand} {material} {color_name}" onClose={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Bulk edit spools')).toBeInTheDocument());

    expect(screen.getByText(/1 of them has a weight entered by hand/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('Unlock weight'));
    fireEvent.click(screen.getByText('Apply to 2'));

    await waitFor(() => expect(api.bulkUpdateSpools).toHaveBeenCalledTimes(1));
    const [ids, fields] = (api.bulkUpdateSpools as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(ids).toEqual([1, 2]);
    expect(fields).toEqual({ weight_locked: false });
  });

  it('shows "— varies —" for fields that differ across the selection', async () => {
    render(<BulkEditSpoolsModal isOpen spools={spools} allSpools={spools} catalogEntries={[]} spoolDisplayTemplate="{brand} {material} {color_name}" onClose={vi.fn()} onSaved={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Bulk edit spools')).toBeInTheDocument());
    // color_name differs (Red vs Blue) → its input placeholder is the "varies" marker.
    expect(screen.getAllByPlaceholderText('— varies —').length).toBeGreaterThan(0);
  });
});
