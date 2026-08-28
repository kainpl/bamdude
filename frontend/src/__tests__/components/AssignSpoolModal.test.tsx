import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { AssignSpoolModal } from '../../components/AssignSpoolModal';
import { api } from '../../api/client';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getSpools: vi.fn(),
    getAssignments: vi.fn(),
    assignSpool: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
    getReplacementWindow: vi.fn().mockResolvedValue({ mode: 'none', pause_layer: null }),
  },
}));

const defaultProps = {
  isOpen: true,
  onClose: vi.fn(),
  printerId: 1,
  amsId: 0,
  trayId: 0,
  trayInfo: { type: 'PLA', color: 'FF0000', location: 'AMS 1 - Slot 1' },
};

const manualSpool = {
  id: 1,
  material: 'PLA',
  subtype: 'Basic',
  brand: 'Polymaker',
  color_name: 'Red',
  rgba: 'FF0000FF',
  label_weight: 1000,
  weight_used: 0,
  tag_uid: null,
  tray_uuid: null,
};

const blSpool = {
  id: 2,
  material: 'PLA',
  subtype: 'Basic',
  brand: 'Bambu',
  color_name: 'Jade White',
  rgba: 'FFFFFFFE',
  label_weight: 1000,
  weight_used: 50,
  tag_uid: '05CC1E0F00000100',
  tray_uuid: 'A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4',
};

const anotherManualSpool = {
  id: 3,
  // Material kept matching trayInfo.type (PLA) so these RFID-filter tests aren't
  // secondarily gated by the material-overlap filter introduced with upstream
  // #1047 — the point of the next three tests is tag_uid/tray_uuid vs manual,
  // not PLA vs PETG.
  material: 'PLA',
  subtype: 'Matte',
  brand: 'Overture',
  color_name: 'Black',
  rgba: '000000FF',
  label_weight: 1000,
  weight_used: 200,
  tag_uid: null,
  tray_uuid: null,
};

describe('AssignSpoolModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getSpools as ReturnType<typeof vi.fn>).mockResolvedValue([manualSpool, blSpool, anotherManualSpool]);
    (api.getAssignments as ReturnType<typeof vi.fn>).mockResolvedValue([]);
  });

  it('renders nothing when closed', () => {
    render(<AssignSpoolModal {...defaultProps} isOpen={false} />);
    expect(screen.queryByText('Assign Spool')).not.toBeInTheDocument();
  });

  it('lists every vendor including Bambu Lab spools (#1133)', async () => {
    render(<AssignSpoolModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText(/Polymaker/)).toBeInTheDocument();
    });

    // Manual spools should be visible
    expect(screen.getByText(/Polymaker/)).toBeInTheDocument();
    expect(screen.getByText(/Overture/)).toBeInTheDocument();

    // BL spool with tag_uid/tray_uuid should ALSO be visible — the earlier
    // "manual spools only" gate (tag_uid && tray_uuid both null) was the
    // exact bug fixed by upstream #1133. (Color name may render in both
    // the row label and the swatch hint, so use getAllByText.)
    expect(screen.getAllByText(/Jade White/).length).toBeGreaterThan(0);
  });

  it('filters out spools already assigned to other slots', async () => {
    (api.getAssignments as ReturnType<typeof vi.fn>).mockResolvedValue([
      { id: 1, spool_id: 3, printer_id: 1, ams_id: 0, tray_id: 1 }, // spool 3 assigned to different slot
    ]);

    render(<AssignSpoolModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText(/Polymaker/)).toBeInTheDocument();
    });

    // Spool 1 (not assigned) should be visible
    expect(screen.getByText(/Polymaker/)).toBeInTheDocument();

    // Spool 3 (assigned to another slot) should NOT be visible
    expect(screen.queryByText(/Overture/)).not.toBeInTheDocument();
  });

  it('keeps spool visible if assigned to the current slot', async () => {
    (api.getAssignments as ReturnType<typeof vi.fn>).mockResolvedValue([
      { id: 1, spool_id: 1, printer_id: 1, ams_id: 0, tray_id: 0 }, // spool 1 assigned to THIS slot
    ]);

    render(<AssignSpoolModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText(/Polymaker/)).toBeInTheDocument();
    });

    // Spool 1 (assigned to current slot) should still be visible for re-assignment
    expect(screen.getByText(/Polymaker/)).toBeInTheDocument();
  });

  it('shows noAvailableSpools message when inventory is empty', async () => {
    (api.getSpools as ReturnType<typeof vi.fn>).mockResolvedValue([]);

    render(<AssignSpoolModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText(/No spools available/i)).toBeInTheDocument();
    });
  });

  it('a paused print asks "replacement or correction?" before assigning', async () => {
    // Mid-pause the same gesture means two opposite things (a physical swap
    // must split the usage at the current layer; a wrong-link fix must not).
    // The prompt is the disambiguation — nothing fires until it's answered.
    const { fireEvent } = await import('@testing-library/react');
    (api.getReplacementWindow as ReturnType<typeof vi.fn>).mockResolvedValue({ mode: 'prompt', pause_layer: 12 });
    (api.assignSpool as ReturnType<typeof vi.fn>).mockResolvedValue({ id: 1 });

    render(<AssignSpoolModal {...defaultProps} />);
    await waitFor(() => expect(screen.getByText(/Polymaker/)).toBeInTheDocument());

    fireEvent.click(screen.getByText(/Polymaker/));
    const buttons = screen.getAllByRole('button', { name: /Assign Spool/ });
    fireEvent.click(buttons[buttons.length - 1]);

    await waitFor(() => expect(screen.getByText('The printer is paused mid-print')).toBeInTheDocument());
    expect(api.assignSpool).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /split the usage/ }));
    await waitFor(() =>
      expect(api.assignSpool).toHaveBeenCalledWith(expect.objectContaining({ mid_print_replacement: true }))
    );
  });

  it('an idle printer assigns without the prompt', async () => {
    const { fireEvent } = await import('@testing-library/react');
    // clearAllMocks does not undo mockResolvedValue — pin the state explicitly
    // so the previous test's 'prompt' window cannot leak in.
    (api.getReplacementWindow as ReturnType<typeof vi.fn>).mockResolvedValue({ mode: 'none', pause_layer: null });
    (api.assignSpool as ReturnType<typeof vi.fn>).mockResolvedValue({ id: 1 });

    render(<AssignSpoolModal {...defaultProps} />);
    await waitFor(() => expect(screen.getByText(/Polymaker/)).toBeInTheDocument());

    fireEvent.click(screen.getByText(/Polymaker/));
    const buttons = screen.getAllByRole('button', { name: /Assign Spool/ });
    fireEvent.click(buttons[buttons.length - 1]);

    await waitFor(() =>
      expect(api.assignSpool).toHaveBeenCalledWith(expect.objectContaining({ mid_print_replacement: false }))
    );
    expect(screen.queryByText('The printer is paused mid-print')).not.toBeInTheDocument();
  });

  it('running after a pause offers the split as a default-off checkbox', async () => {
    // Real workflow: pause -> swap -> resume at the printer, THEN the UI.
    // No modal here — bulk wrong-link corrections must stay friction-free —
    // but ticking the box declares the swap and splits at the pause layer.
    const { fireEvent } = await import('@testing-library/react');
    (api.getReplacementWindow as ReturnType<typeof vi.fn>).mockResolvedValue({ mode: 'optin', pause_layer: 87 });
    (api.assignSpool as ReturnType<typeof vi.fn>).mockResolvedValue({ id: 1 });

    render(<AssignSpoolModal {...defaultProps} />);
    await waitFor(() => expect(screen.getByText(/Polymaker/)).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText(/paused at layer 87/)).toBeInTheDocument());

    fireEvent.click(screen.getByText(/Polymaker/));

    // Unticked: a plain assignment — the correction path, no prompt.
    const buttons = screen.getAllByRole('button', { name: /Assign Spool/ });
    fireEvent.click(buttons[buttons.length - 1]);
    await waitFor(() =>
      expect(api.assignSpool).toHaveBeenCalledWith(expect.objectContaining({ mid_print_replacement: false }))
    );
    expect(screen.queryByText('The printer is paused mid-print')).not.toBeInTheDocument();
  });

  it('the ticked checkbox declares the replacement', async () => {
    const { fireEvent } = await import('@testing-library/react');
    (api.getReplacementWindow as ReturnType<typeof vi.fn>).mockResolvedValue({ mode: 'optin', pause_layer: 87 });
    (api.assignSpool as ReturnType<typeof vi.fn>).mockResolvedValue({ id: 1 });

    render(<AssignSpoolModal {...defaultProps} />);
    await waitFor(() => expect(screen.getByText(/Polymaker/)).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText(/paused at layer 87/)).toBeInTheDocument());

    fireEvent.click(screen.getByText(/Polymaker/));
    fireEvent.click(screen.getByText(/paused at layer 87/));
    const buttons = screen.getAllByRole('button', { name: /Assign Spool/ });
    fireEvent.click(buttons[buttons.length - 1]);
    await waitFor(() =>
      expect(api.assignSpool).toHaveBeenCalledWith(expect.objectContaining({ mid_print_replacement: true }))
    );
  });

  it('drops archived spools always — even with the toggle on', async () => {
    const archivedSpool = { ...manualSpool, id: 99, archived_at: '2026-01-01T00:00:00Z', brand: 'Archived' };
    (api.getSpools as ReturnType<typeof vi.fn>).mockResolvedValue([archivedSpool]);

    render(<AssignSpoolModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText(/No spools available/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/Archived/)).not.toBeInTheDocument();
  });
});
