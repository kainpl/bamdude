/**
 * The spool's number in the slot-assignment picker (upstream 632930f02, f652d3e0a).
 *
 * The tile carries the number printed on the label, and a `#n` search that
 * finds nothing says why — the server answers the reason for the built-in
 * inventory; the Spoolman list is whole in the browser, so it is read there.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { AssignSpoolModal } from '../../components/AssignSpoolModal';
import { api } from '../../api/client';
import { spoolMatchesQuery } from '../../utils/inventorySearch';
import type { InventorySpool } from '../../api/client';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getCurrentUser: vi.fn().mockResolvedValue({ id: 1, username: 'admin', is_admin: true, permissions: [] }),
    getSpools: vi.fn(),
    getSpoolPicker: vi.fn(),
    getSpool: vi.fn(),
    getAssignments: vi.fn().mockResolvedValue([]),
    assignSpool: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    getUiPreferences: vi.fn().mockResolvedValue({}),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
    getReplacementWindow: vi.fn().mockResolvedValue({ mode: 'none', pause_layer: null }),
    getSpoolmanInventorySpools: vi.fn().mockResolvedValue([]),
    getSpoolmanSlotAssignments: vi.fn().mockResolvedValue([]),
    assignSpoolmanSlot: vi.fn(),
  },
}));

const props = {
  isOpen: true,
  onClose: vi.fn(),
  printerId: 1,
  amsId: 0,
  trayId: 0,
  trayInfo: { type: 'PLA', color: 'FF0000', location: 'AMS 1 - Slot 1' },
};

const spool = (id: number, extra: Record<string, unknown> = {}) =>
  ({
    id,
    material: 'PLA',
    subtype: 'Basic',
    brand: 'Polymaker',
    color_name: 'Red',
    rgba: 'FF0000FF',
    label_weight: 1000,
    weight_used: 0,
    ...extra,
  }) as unknown as InventorySpool;

const page = (items: InventorySpool[], miss: unknown = null) => ({
  items,
  meta: { total: items.length, current_page: 1, per_page: 50, last_page: 1 },
  miss,
});

const search = (text: string) =>
  fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: text } });

describe('spoolMatchesQuery — #id', () => {
  it('a hash number is that spool exactly; a bare number is a substring', () => {
    expect(spoolMatchesQuery(spool(12), '#12')).toBe(true);
    expect(spoolMatchesQuery(spool(123), '#12')).toBe(false);
    expect(spoolMatchesQuery(spool(123), '12')).toBe(true);
    expect(spoolMatchesQuery(spool(5, { note: 'shelf #A7' }), '#A7')).toBe(true);
  });
});

describe('AssignSpoolModal — the spool number', () => {
  beforeEach(() => vi.clearAllMocks());

  it('every tile shows the number', async () => {
    vi.mocked(api.getSpoolPicker).mockResolvedValue(page([spool(7), spool(42)]) as never);
    render(<AssignSpoolModal {...props} />);
    expect(await screen.findByText('#7')).toBeInTheDocument();
    expect(screen.getByText('#42')).toBeInTheDocument();
  });

  it('a numbered miss names the slot that holds the spool', async () => {
    vi.mocked(api.getSpoolPicker).mockImplementation((async (params: { q?: string }) =>
      params.q === '#9'
        ? page([], { reason: 'other_slot', spool_id: 9, printer_id: 2, printer_name: 'Bench', ams_id: 1, tray_id: 2 })
        : page([spool(7)])) as never);
    render(<AssignSpoolModal {...props} />);
    await screen.findByText('#7');
    search('#9');
    expect(await screen.findByText('Spool #9 is in another slot: Bench · B3.')).toBeInTheDocument();
  });

  it('an archived and an unknown number each say so', async () => {
    vi.mocked(api.getSpoolPicker).mockImplementation((async (params: { q?: string }) => {
      if (params.q === '#9') return page([], { reason: 'archived', spool_id: 9 });
      if (params.q === '#10') return page([], { reason: 'not_found', spool_id: 10 });
      return page([spool(7)]);
    }) as never);
    render(<AssignSpoolModal {...props} />);
    await screen.findByText('#7');
    search('#9');
    expect(await screen.findByText('Spool #9 is archived. Restore it to assign it.')).toBeInTheDocument();
    search('#10');
    expect(await screen.findByText('There is no spool #10.')).toBeInTheDocument();
  });

  it('Spoolman mode: a number held by another slot says where', async () => {
    vi.mocked(api.getSpoolmanInventorySpools).mockResolvedValue([spool(7), spool(9)] as never);
    vi.mocked(api.getSpoolmanSlotAssignments).mockResolvedValue([
      { printer_id: 2, printer_name: 'Bench', ams_id: 1, tray_id: 2, spoolman_spool_id: 9 },
    ] as never);
    render(<AssignSpoolModal {...props} spoolmanEnabled />);
    expect(await screen.findByText('#7')).toBeInTheDocument();
    expect(screen.queryByText('#9')).toBeNull();
    search('#9');
    expect(await screen.findByText('Spool #9 is in another slot: Bench · B3.')).toBeInTheDocument();
    search('#77');
    expect(await screen.findByText('There is no active spool #77.')).toBeInTheDocument();
  });
});
