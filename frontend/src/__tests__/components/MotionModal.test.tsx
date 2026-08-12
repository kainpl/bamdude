/**
 * Moving the head, the bed and the extruder from the browser.
 *
 * ⚠️ **The signs sent from here are BambuStudio's, unflipped.** Whether Y and Z
 * need inverting depends on the printer's frame — a bed-slinger's Z carries the
 * toolhead, not the bed — and the backend applies that. So the same button
 * sends the same number for every model. "Fixing" the sign here is what upstream
 * #1334 was.
 *
 * ⚠️ **Not-homed refuses X, Y AND Z — but only the first two are parity.**
 * Studio checks the home flags before an X/Y move and returns; for Z it sends
 * the move and advises recentering afterwards. Refusing Z is ours, matching the
 * card's bed-jog control whose "move anyway" was removed upstream because it
 * drove the move with the soft endstops disabled.
 *
 * ⚠️ **An absent home flag means homed**, not unknown-so-refuse — the same
 * sentinel the backend applies. Otherwise the pad greys out on every printer
 * that never reports the field.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MotionModal } from '../../components/MotionModal';
import { api, type PrinterStatus } from '../../api/client';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

const jogAxis = vi.spyOn(api, 'jogAxis');
const disableSteppers = vi.spyOn(api, 'disableSteppers');

function show(over: Partial<PrinterStatus> = {}, props: Record<string, unknown> = {}) {
  const status = {
    state: 'IDLE',
    temperatures: { nozzle: 220, nozzle_2: 210, bed: 60 },
    ...over,
  } as unknown as PrinterStatus;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MotionModal
        printerId={1}
        isOpen
        onClose={() => {}}
        status={status}
        isDualNozzle={false}
        canControl
        {...props}
      />
    </QueryClientProvider>,
  );
}

/** The pad is SVG, where `disabled` is not an attribute — the sectors carry
 *  `aria-disabled` instead. Buttons still use the real thing. */
function blocked(label: string): boolean {
  const el = screen.getByLabelText(label);
  return el.getAttribute('aria-disabled') === 'true' || (el as HTMLButtonElement).disabled === true;
}

beforeEach(() => {
  jogAxis.mockReset();
  jogAxis.mockResolvedValue({ success: true, axis: 'x', distance: 10 });
  disableSteppers.mockReset();
  disableSteppers.mockResolvedValue({ success: true });
});

describe('MotionModal', () => {
  it('sends the toolhead moves with the signs Studio uses', async () => {
    show();

    fireEvent.click(screen.getByLabelText('X +10'));
    await waitFor(() => expect(jogAxis).toHaveBeenCalledWith(1, 'x', 10, 0));

    fireEvent.click(screen.getByLabelText('Y -1'));
    await waitFor(() => expect(jogAxis).toHaveBeenCalledWith(1, 'y', -1, 0));
  });

  it('treats negative Z as closing the gap, which is Studio "up"', async () => {
    show();

    fireEvent.click(screen.getByLabelText('Z -10'));

    await waitFor(() => expect(jogAxis).toHaveBeenCalledWith(1, 'z', -10, 0));
  });

  it('draws one round pad carrying all eight directions and Home', () => {
    /* ⚠️ Pins the SHAPE, not just the buttons. An earlier version split the two
       rings across two square grids, which parked X-1 and X+1 in a strip under
       the cross where they read as belonging to nothing — and every label-based
       assertion here passed anyway. */
    show();

    /* ⚠️ Looked up by label rather than with `querySelector`: in jsdom an
       attribute selector whose value contains "+" silently matches nothing, so
       `[aria-label="X +10"]` returns null for an element that is right there. */
    for (const label of ['X -10', 'X -1', 'X +1', 'X +10', 'Y -10', 'Y -1', 'Y +1', 'Y +10']) {
      expect(screen.getByLabelText(label).tagName.toLowerCase()).toBe('path');
    }
    expect(screen.getByLabelText('printers.motion.home').tagName.toLowerCase()).toBe('circle');
  });

  it('offers only the two step sizes the protocol can tell apart', () => {
    /* A free input would promise a precision `xyz_ctrl` never receives. */
    show();

    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();
    expect(screen.getByLabelText('X +1')).toBeInTheDocument();
    expect(screen.getByLabelText('X +10')).toBeInTheDocument();
  });

  it('keeps everything usable when the printer never reports a home flag', () => {
    show();

    expect(blocked('X +10')).toBe(false);
    expect(blocked('Z -10')).toBe(false);
  });

  it('refuses X and Y when the printer says they are not homed', () => {
    show({ axis_at_home: { x: false, y: false, z: true } });

    expect(blocked('X +10')).toBe(true);
    expect(blocked('Y +10')).toBe(true);
  });

  it('leaves Home reachable while the moves are blocked', () => {
    /* Being unhomed is exactly what Home fixes — disabling it there would be a
       dead end. */
    show({ axis_at_home: { x: false, y: false, z: false } });

    expect(blocked('printers.motion.home')).toBe(false);
  });

  it('refuses Z as well, which the card already did', () => {
    show({ axis_at_home: { x: true, y: true, z: false } });

    expect(blocked('Z -10')).toBe(true);
  });

  it('refuses the extruder below 170 °C', () => {
    show({ temperatures: { nozzle: 30, bed: 25 } });

    expect(screen.getByLabelText('printers.motion.retract')).toBeDisabled();
    expect(screen.getByText(/printers.motion.tooCold/)).toBeInTheDocument();
  });

  it('allows the extruder once it is hot', async () => {
    show();

    fireEvent.click(screen.getByLabelText('printers.motion.extrude'));

    await waitFor(() => expect(jogAxis).toHaveBeenCalledWith(1, 'e', 10, 0));
  });

  it('reads the deputy nozzle temperature when the deputy is selected', () => {
    /* Checking the main nozzle would let a cold extruder run because its
       neighbour was warm. */
    show({ temperatures: { nozzle: 220, nozzle_2: 30, bed: 60 } }, { isDualNozzle: true });

    fireEvent.click(screen.getByText('printers.motion.auxiliary'));

    expect(screen.getByLabelText('printers.motion.retract')).toBeDisabled();
  });

  it('addresses the selected extruder', async () => {
    show({}, { isDualNozzle: true });

    fireEvent.click(screen.getByText('printers.motion.auxiliary'));
    fireEvent.click(screen.getByLabelText('printers.motion.extrude'));

    await waitFor(() => expect(jogAxis).toHaveBeenCalledWith(1, 'e', 10, 1));
  });

  it('stops everything while a job is on the printer', () => {
    show({ state: 'RUNNING' });

    expect(blocked('X +10')).toBe(true);
    expect(blocked('Z -10')).toBe(true);
    expect(blocked('printers.motion.extrude')).toBe(true);
    expect(screen.getByText('printers.motion.printingBlocked')).toBeInTheDocument();
  });

  it('releases the motors on request', async () => {
    show();

    fireEvent.click(screen.getByText('printers.motion.releaseMotors'));

    await waitFor(() => expect(disableSteppers).toHaveBeenCalledWith(1));
  });

  it('shows nothing operable without the permission', () => {
    show({}, { canControl: false });

    expect(blocked('X +10')).toBe(true);
    expect(screen.getByText('printers.motion.releaseMotors')).toBeDisabled();
  });
});
