/**
 * «Assign to slot» from the spool form (upstream 632930f02 leg 3).
 *
 * The operator starts from the spool and picks printer → AMS unit → slot. The
 * assignment itself is NOT a second writer: the chooser ends in the same
 * dialog every slot assignment goes through (`AssignSpoolModal`, opened with
 * the spool already decided), so the mid-print «replacement or correction?»
 * question, the material-mismatch confirmation and the archived-spool refusal
 * are asked exactly as from the printer card.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import { render } from '../utils';
import { AssignSpoolModal } from '../../components/AssignSpoolModal';
import { AssignToSlotModal } from '../../components/AssignToSlotModal';
import { api } from '../../api/client';
import i18n from '../../i18n';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getCurrentUser: vi.fn().mockResolvedValue({ id: 1, username: 'admin', is_admin: true, permissions: [] }),
    getSpoolPicker: vi.fn(),
    getSpool: vi.fn(),
    getAssignments: vi.fn().mockResolvedValue([]),
    assignSpool: vi.fn(),
    assignSpoolmanSlot: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    getUiPreferences: vi.fn().mockResolvedValue({}),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
    getReplacementWindow: vi.fn().mockResolvedValue({ mode: 'none', pause_layer: null }),
    getSpoolmanInventorySpools: vi.fn().mockResolvedValue([]),
    getSpoolmanSlotAssignments: vi.fn().mockResolvedValue([]),
    getPrinters: vi.fn(),
    getPrinterStatus: vi.fn(),
  },
}));

const SPOOL = { id: 42, displayName: 'Acme PLA Red', source: 'inventory' as const };
const record = (extra: Record<string, unknown> = {}) => ({
  id: 42, material: 'PLA', brand: 'Acme', color_name: 'Red', rgba: 'FF0000FF',
  label_weight: 1000, weight_used: 0, archived_at: null, ...extra,
});

const slot = {
  isOpen: true,
  printerId: 7,
  amsId: 0,
  trayId: 1,
  trayInfo: { type: 'PLA', material: 'PLA', profile: 'PLA', color: 'FF0000', location: 'AMS-A Slot 2' },
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getSpool).mockResolvedValue(record() as never);
  vi.mocked(api.assignSpool).mockResolvedValue({ id: 1, spool_id: 42, printer_id: 7, ams_id: 0, tray_id: 1 } as never);
  vi.mocked(api.getReplacementWindow).mockResolvedValue({ mode: 'none', pause_layer: null } as never);
  vi.mocked(api.getAssignments).mockResolvedValue([] as never);
});

describe('AssignSpoolModal with the spool already decided', () => {
  const open = (props: Record<string, unknown> = {}) =>
    render(<AssignSpoolModal {...slot} onClose={vi.fn()} fixedSpool={SPOOL} {...props} />);
  const assignButton = () => screen.getByRole('button', { name: /^Assign Spool$/ });

  it('names the spool and offers no list to pick from', async () => {
    open();
    expect(await screen.findByText('Acme PLA Red')).toBeInTheDocument();
    expect(screen.getByText('#42')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/search/i)).toBeNull();
    expect(screen.queryByText(/Show all spools/i)).toBeNull();
    expect(api.getSpoolPicker).not.toHaveBeenCalled();
  });

  it('assigns through the same route, with the slot it was opened for', async () => {
    const onAssigned = vi.fn();
    open({ onAssigned });
    fireEvent.click(assignButton());
    await waitFor(() => expect(api.assignSpool).toHaveBeenCalledTimes(1));
    expect(api.assignSpool).toHaveBeenCalledWith({
      spool_id: 42, printer_id: 7, ams_id: 0, tray_id: 1, mid_print_replacement: false,
    });
    await waitFor(() => expect(onAssigned).toHaveBeenCalledTimes(1));
  });

  it('a paused print is asked «replacement or correction?» before anything is written', async () => {
    vi.mocked(api.getReplacementWindow).mockResolvedValue({ mode: 'prompt', pause_layer: 12 } as never);
    open();
    await waitFor(() => expect(api.getReplacementWindow).toHaveBeenCalledWith(7, 0, 1));
    await waitFor(() => expect(assignButton()).toBeEnabled());
    fireEvent.click(assignButton());
    await waitFor(() => expect(screen.getAllByRole('dialog').length).toBeGreaterThan(1));
    expect(api.assignSpool).not.toHaveBeenCalled();
  });

  it('a spool of another material asks for confirmation first', async () => {
    vi.mocked(api.getSpool).mockResolvedValue(record({ material: 'PETG' }) as never);
    open();
    fireEvent.click(assignButton());
    expect(await screen.findByRole('button', { name: /Assign Anyway/i })).toBeInTheDocument();
    expect(api.assignSpool).not.toHaveBeenCalled();
  });

  it('an archived spool is refused', async () => {
    vi.mocked(api.getSpool).mockResolvedValue(record({ archived_at: '2026-01-01T00:00:00Z' }) as never);
    open();
    fireEvent.click(assignButton());
    await waitFor(() => expect(api.getSpool).toHaveBeenCalled());
    expect(api.assignSpool).not.toHaveBeenCalled();
  });

  it('a Spoolman spool goes through the Spoolman route', async () => {
    vi.mocked(api.assignSpoolmanSlot).mockResolvedValue({} as never);
    open({ spoolmanEnabled: true, fixedSpool: { ...SPOOL, source: 'spoolman' } });
    fireEvent.click(assignButton());
    await waitFor(() => expect(api.assignSpoolmanSlot).toHaveBeenCalledWith({
      spoolman_spool_id: 42, printer_id: 7, ams_id: 0, tray_id: 1, mid_print_replacement: false,
    }));
    expect(api.assignSpool).not.toHaveBeenCalled();
  });
});

describe('AssignToSlotModal — printer → AMS unit → slot', () => {
  const STATUS = {
    connected: true,
    ams: [
      {
        id: 0, is_ams_ht: false,
        tray: [
          { id: 0, tray_type: 'PLA', tray_color: '00FF00FF', tray_sub_brands: 'PLA Basic', tray_uuid: 'A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4' },
          { id: 1, tray_type: 'PETG', tray_color: 'FF0000FF', tray_sub_brands: 'PETG HF' },
          { id: 2, tray_type: null, tray_color: null, tray_sub_brands: null },
          { id: 3, tray_type: null, tray_color: null, tray_sub_brands: null },
        ],
      },
      { id: 128, is_ams_ht: true, tray: [{ id: 0, tray_type: 'PA', tray_color: '111111FF', tray_sub_brands: 'PA' }] },
    ],
    vt_tray: [{ id: 254, tray_type: null, tray_color: null, tray_sub_brands: null }],
  };

  beforeEach(() => {
    vi.mocked(api.getPrinters).mockResolvedValue([
      { id: 7, name: 'Bench', model: 'X1C' },
      { id: 8, name: 'Shelf', model: 'P1S' },
    ] as never);
    vi.mocked(api.getPrinterStatus).mockResolvedValue(STATUS as never);
  });

  const open = (onClose = vi.fn()) => {
    render(<AssignToSlotModal spool={SPOOL} spoolmanMode={false} onClose={onClose} />);
    return onClose;
  };

  /** Choose a printer once the list has arrived. */
  const pickPrinter = async (name: string, id: number) => {
    const printer = await screen.findByRole('combobox', { name: 'Printer' });
    await waitFor(() => expect(within(printer).getByRole('option', { name })).toBeInTheDocument());
    fireEvent.change(printer, { target: { value: String(id) } });
  };

  it('offers the printers, then the units of the chosen one, then its slots', async () => {
    open();
    const printer = await screen.findByRole('combobox', { name: 'Printer' });
    await waitFor(() => expect(within(printer).getByRole('option', { name: 'Bench' })).toBeInTheDocument());
    expect(screen.queryByRole('combobox', { name: 'AMS unit' })).toBeNull();

    fireEvent.change(printer, { target: { value: '7' } });
    const unit = await screen.findByRole('combobox', { name: 'AMS unit' });
    expect(within(unit).getAllByRole('option').map((o) => o.textContent)).toEqual(['AMS-A', 'HT-A', 'External']);
    // The first unit is chosen: its four slots are offered.
    expect(await screen.findByRole('button', { name: /A2.*PETG HF/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /A3.*Empty/ })).toBeInTheDocument();
  });

  it('a slot holding a Bambu Lab RFID spool is not offered', async () => {
    open();
    await pickPrinter('Bench', 7);
    const rfid = await screen.findByRole('button', { name: /A1.*PLA Basic/ });
    expect(rfid).toBeDisabled();
    expect(rfid).toHaveTextContent(/RFID/);
  });

  it('a chosen slot opens the usual assignment dialog for this spool and that slot', async () => {
    const onClose = open();
    await pickPrinter('Bench', 7);
    fireEvent.click(await screen.findByRole('button', { name: /A2.*PETG HF/ }));

    // The spool is PLA, the slot reports PETG: the usual confirmation asks.
    fireEvent.click(await screen.findByRole('button', { name: /^Assign Spool$/ }));
    expect(await screen.findByRole('button', { name: /Assign Anyway/i })).toBeInTheDocument();
    expect(api.assignSpool).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /Assign Anyway/i }));
    await waitFor(() => expect(api.assignSpool).toHaveBeenCalledWith({
      spool_id: 42, printer_id: 7, ams_id: 0, tray_id: 1, mid_print_replacement: false,
    }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('an external holder is addressed as the printer card addresses it', async () => {
    open();
    await pickPrinter('Bench', 7);
    fireEvent.change(await screen.findByRole('combobox', { name: 'AMS unit' }), { target: { value: '255' } });
    fireEvent.click(await screen.findByRole('button', { name: /Ext/ }));
    fireEvent.click(await screen.findByRole('button', { name: /^Assign Spool$/ }));
    // An empty holder reports no material, so the usual confirmation asks here too.
    fireEvent.click(await screen.findByRole('button', { name: /Assign Anyway/i }));
    await waitFor(() => expect(api.assignSpool).toHaveBeenCalledWith({
      spool_id: 42, printer_id: 7, ams_id: 255, tray_id: 0, mid_print_replacement: false,
    }));
  });

  it('a slot that already holds a spool opens the dialog in replace mode', async () => {
    vi.mocked(api.getAssignments).mockResolvedValue([
      { id: 5, spool_id: 9, printer_id: 7, ams_id: 0, tray_id: 1, spool: { id: 9, material: 'PETG', brand: 'Other', label_weight: 1000, weight_used: 0 } },
    ] as never);
    open();
    await pickPrinter('Bench', 7);
    const occupied = await screen.findByRole('button', { name: /A2.*PETG HF/ });
    await waitFor(() => expect(occupied).toHaveTextContent(/Other PETG/));
    fireEvent.click(occupied);
    expect(await screen.findByRole('button', { name: /^Replace spool$/i })).toBeInTheDocument();
  });

  it('names the chosen slot in the language of the reader', async () => {
    await i18n.changeLanguage('uk');
    try {
      open();
      const [printer] = await screen.findAllByRole('combobox');
      await waitFor(() => expect(within(printer).getByRole('option', { name: 'Bench' })).toBeInTheDocument());
      fireEvent.change(printer, { target: { value: '7' } });
      fireEvent.click(await screen.findByRole('button', { name: /A2.*PETG HF/ }));
      expect(await screen.findByText('(AMS-A Слот 2)')).toBeInTheDocument();
    } finally {
      await i18n.changeLanguage('en');
    }
  });

  it('says so when the printer reports no slots', async () => {
    vi.mocked(api.getPrinterStatus).mockResolvedValue({ connected: false, ams: [], vt_tray: [] } as never);
    open();
    await pickPrinter('Shelf', 8);
    expect(await screen.findByText(/reports no slots/i)).toBeInTheDocument();
  });
});
