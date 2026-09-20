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
    getSpoolmanInventorySpools: vi.fn().mockResolvedValue([]),
    getSpoolmanSlotAssignments: vi.fn().mockResolvedValue([]),
    assignSpoolmanSlot: vi.fn(),
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

  it('running after a pause asks through the same modal', async () => {
    // Real workflow: pause -> swap -> resume at the printer, THEN the UI.
    // One question in one place: this window used to be a default-off toggle
    // in the picker, which was routinely missed — now the same modal opens,
    // worded for the pause that is already behind the print.
    const { fireEvent } = await import('@testing-library/react');
    (api.getReplacementWindow as ReturnType<typeof vi.fn>).mockResolvedValue({ mode: 'optin', pause_layer: 87 });
    (api.assignSpool as ReturnType<typeof vi.fn>).mockResolvedValue({ id: 1 });

    render(<AssignSpoolModal {...defaultProps} />);
    await waitFor(() => expect(screen.getByText(/Polymaker/)).toBeInTheDocument());

    fireEvent.click(screen.getByText(/Polymaker/));
    const buttons = screen.getAllByRole('button', { name: /Assign Spool/ });
    fireEvent.click(buttons[buttons.length - 1]);

    await waitFor(() => expect(screen.getByText('This print had a pause behind it')).toBeInTheDocument());
    expect(screen.getByText(/paused at layer 87/)).toBeInTheDocument();
    expect(screen.queryByText('The printer is paused mid-print')).not.toBeInTheDocument();
    expect(api.assignSpool).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /fixing a wrong link/ }));
    await waitFor(() =>
      expect(api.assignSpool).toHaveBeenCalledWith(expect.objectContaining({ mid_print_replacement: false }))
    );
  });

  it('declaring the replacement in that modal splits at the pause layer', async () => {
    const { fireEvent } = await import('@testing-library/react');
    (api.getReplacementWindow as ReturnType<typeof vi.fn>).mockResolvedValue({ mode: 'optin', pause_layer: 87 });
    (api.assignSpool as ReturnType<typeof vi.fn>).mockResolvedValue({ id: 1 });

    render(<AssignSpoolModal {...defaultProps} />);
    await waitFor(() => expect(screen.getByText(/Polymaker/)).toBeInTheDocument());

    fireEvent.click(screen.getByText(/Polymaker/));
    const buttons = screen.getAllByRole('button', { name: /Assign Spool/ });
    fireEvent.click(buttons[buttons.length - 1]);
    await waitFor(() => expect(screen.getByText('This print had a pause behind it')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /split the usage/ }));
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

  // Replace mode (spec 2026-09-13 §3.2). The dialog is the same dialog; being
  // told what is currently on the slot is what turns it into a replace.
  describe('opened over an assigned slot', () => {
    // Deliberately NOT the string the list composes for spool 7 — the page
    // passes the name the hover card shows, and keeping it distinct is how
    // "the current spool is not in the list" can be asserted at all.
    const currentSpool = { id: 7, displayName: 'PLA Red #7', source: 'inventory' as const };
    const onSlotSpool = { ...manualSpool, id: 7, brand: 'Polymaker', color_name: 'Red' };
    const freeSpool = { ...manualSpool, id: 8, brand: 'Overture', color_name: 'Black' };

    beforeEach(() => {
      (api.getReplacementWindow as ReturnType<typeof vi.fn>).mockResolvedValue({ mode: 'none', pause_layer: null });
      (api.getSpools as ReturnType<typeof vi.fn>).mockResolvedValue([onSlotSpool, freeSpool]);
      (api.getAssignments as ReturnType<typeof vi.fn>).mockResolvedValue([
        { id: 1, spool_id: 7, printer_id: 1, ams_id: 0, tray_id: 0 },
      ]);
      (api.assignSpool as ReturnType<typeof vi.fn>).mockResolvedValue({
        id: 2,
        spool_id: 8,
        printer_id: 1,
        ams_id: 0,
        tray_id: 0,
        replaced_spool_id: 7,
      });
    });

    it('names the current spool and offers Replace instead of Assign', async () => {
      render(<AssignSpoolModal {...defaultProps} currentSpool={currentSpool} />);

      await waitFor(() => expect(screen.getByText(/Overture/)).toBeInTheDocument());

      expect(screen.getByRole('heading', { name: 'Replace spool' })).toBeInTheDocument();
      expect(screen.getByText(/Currently assigned/)).toBeInTheDocument();
      expect(screen.getByText('PLA Red #7')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Replace spool/ })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Assign Spool/ })).not.toBeInTheDocument();
    });

    it('hides the spool it would replace — re-picking it is a no-op', async () => {
      // The slot's own spool is normally KEPT in the list (a re-assign of the
      // same spool is idempotent), so this exclusion is replace-mode only.
      render(<AssignSpoolModal {...defaultProps} currentSpool={currentSpool} />);

      await waitFor(() => expect(screen.getByText(/Overture/)).toBeInTheDocument());

      expect(screen.queryByText(/Polymaker/)).not.toBeInTheDocument();
    });

    it('keeps the current spool hidden even with "Show all spools" on', async () => {
      const { fireEvent } = await import('@testing-library/react');
      render(<AssignSpoolModal {...defaultProps} currentSpool={currentSpool} />);

      await waitFor(() => expect(screen.getByText(/Overture/)).toBeInTheDocument());
      fireEvent.click(screen.getByLabelText(/Show all spools/i));

      // Pin that the toggle actually went on — otherwise a click that silently
      // stopped reaching the checkbox would leave this test asserting the
      // default state and passing for the wrong reason.
      expect(screen.getByLabelText(/Show all spools/i)).toBeChecked();
      await waitFor(() => expect(screen.getByText(/Overture/)).toBeInTheDocument());
      expect(screen.queryByText(/Polymaker/)).not.toBeInTheDocument();
    });

    it('confirming sends one plain assign and reports both names', async () => {
      const { fireEvent } = await import('@testing-library/react');
      render(<AssignSpoolModal {...defaultProps} currentSpool={currentSpool} />);

      await waitFor(() => expect(screen.getByText(/Overture/)).toBeInTheDocument());
      fireEvent.click(screen.getByText(/Overture/));
      fireEvent.click(screen.getByRole('button', { name: /Replace spool/ }));

      await waitFor(() =>
        expect(api.assignSpool).toHaveBeenCalledWith({
          spool_id: 8,
          printer_id: 1,
          ams_id: 0,
          tray_id: 0,
          mid_print_replacement: false,
        })
      );
      expect(api.assignSpool).toHaveBeenCalledTimes(1);
      await waitFor(() =>
        expect(screen.getByText('PLA Red #7 replaced with Overture PLA Black')).toBeInTheDocument()
      );
    });

    it('keeps the pending-config hint when the slot is empty right now', async () => {
      // Replacing a LINK over a slot whose filament is not loaded yet is still
      // a pending assignment — the toast must say so, or the operator reads
      // "replaced" and expects the AMS to be configured already. Replace mode
      // used to drop that half of the message entirely.
      const { fireEvent } = await import('@testing-library/react');
      (api.assignSpool as ReturnType<typeof vi.fn>).mockResolvedValue({
        id: 2,
        spool_id: 8,
        printer_id: 1,
        ams_id: 0,
        tray_id: 0,
        replaced_spool_id: 7,
        pending_config: true,
      });

      render(<AssignSpoolModal {...defaultProps} currentSpool={currentSpool} />);

      await waitFor(() => expect(screen.getByText(/Overture/)).toBeInTheDocument());
      fireEvent.click(screen.getByText(/Overture/));
      fireEvent.click(screen.getByRole('button', { name: /Replace spool/ }));

      await waitFor(() =>
        expect(
          screen.getByText(
            'PLA Red #7 replaced with Overture PLA Black. The slot will be configured when you insert the filament.'
          )
        ).toBeInTheDocument()
      );
    });

    it('excludes the current Spoolman spool from the Spoolman list', async () => {
      (api.getSpoolmanInventorySpools as ReturnType<typeof vi.fn>).mockResolvedValue([onSlotSpool, freeSpool]);
      (api.getSpoolmanSlotAssignments as ReturnType<typeof vi.fn>).mockResolvedValue([]);

      render(
        <AssignSpoolModal
          {...defaultProps}
          spoolmanEnabled
          currentSpool={{ ...currentSpool, source: 'spoolman' }}
        />
      );

      await waitFor(() => expect(screen.getByText(/Overture/)).toBeInTheDocument());

      expect(screen.queryByText(/Polymaker/)).not.toBeInTheDocument();
      expect(screen.getByRole('heading', { name: 'Replace spool' })).toBeInTheDocument();
    });

    it('without a current spool it is still the assign dialog', async () => {
      render(<AssignSpoolModal {...defaultProps} />);

      await waitFor(() => expect(screen.getByText(/Overture/)).toBeInTheDocument());

      expect(screen.getByRole('heading', { name: 'Assign Spool' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Assign Spool/ })).toBeInTheDocument();
      expect(screen.queryByText(/Currently assigned/)).not.toBeInTheDocument();
      // The slot's own spool stays pickable — that is the pre-existing rule.
      expect(screen.getByText(/Polymaker/)).toBeInTheDocument();
    });
  });

  it('asks about ITS OWN slot, not just the printer', async () => {
    // A replacement charges everything printed so far to the spool that came
    // OUT, so a slot holding nothing cannot be one. Asked about the printer
    // alone, the modal raised "replacement or correction?" every time an empty
    // slot was filled mid-print — a question with no answer. The server needs
    // the slot to refuse it, so the slot has to travel with the question.
    render(<AssignSpoolModal {...defaultProps} amsId={1} trayId={2} />);

    await waitFor(() => expect(api.getReplacementWindow).toHaveBeenCalledWith(1, 1, 2));
  });
});
