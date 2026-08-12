/**
 * The temperature dialog, and the three rules that are easy to get backwards.
 *
 * Registry N6.
 *
 * ⚠️ **Bounds come from the server, never from a constant here.** They depend on
 * the model, on what the printer reported, and on the mains voltage — a 220 V X1
 * accepts a LOWER bed temperature than a 110 V one. A table in the browser would
 * disagree with the backend the moment any of those changed, and the
 * disagreement would surface as a request refused for reasons the UI cannot
 * explain.
 *
 * ⚠️ **A missing `ext_has_nozzle` entry is not `false`.** It means the machine
 * cannot detect a hotend at all — the A and P series — and BambuStudio defaults
 * the flag to installed for exactly that reason. Treating absent as false would
 * grey the control out on most of the fleet.
 *
 * ⚠️ **Off is 0, and 0 is exempt from the range.** That is why there is a
 * separate button rather than "hold minus": stepping stops at the floor, which
 * on a bed is 20 °C, and would leave the heater on.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { TemperatureModal } from '../../components/TemperatureModal';
import { api, type PrinterStatus } from '../../api/client';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

const setTemperature = vi.spyOn(api, 'setTemperature');

function status(over: Partial<PrinterStatus> = {}): PrinterStatus {
  return {
    temperatures: { nozzle: 30, nozzle_target: 0, bed: 25, bed_target: 60 },
    temperature_limits: { nozzle: [20, 300], bed: [20, 110], chamber: [0, 65] },
    ...over,
  } as unknown as PrinterStatus;
}

function show(over: Partial<PrinterStatus> = {}, props: Record<string, unknown> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TemperatureModal
        printerId={1}
        isOpen
        onClose={() => {}}
        status={status(over)}
        isDualNozzle={false}
        supportsChamberHeater={false}
        canControl
        {...props}
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  setTemperature.mockReset();
  setTemperature.mockResolvedValue({ success: true, part: 'bed', target: 0, limits: [20, 110] });
});

describe('TemperatureModal', () => {
  it('shows the bounds the server sent, not a built-in pair', () => {
    show();

    // 110, not the 120 a hardcoded default would have produced.
    expect(screen.getByText('20–110°C')).toBeInTheDocument();
  });

  it('turns a heater off with 0 rather than with the range floor', async () => {
    show();

    // The bed, which is the row actually heating here (target 60).
    fireEvent.click(screen.getAllByText('printers.temperatureControl.turnOff')[1]);

    await waitFor(() => expect(setTemperature).toHaveBeenCalledWith(1, 'bed', 0, 0));
  });

  it('offers nothing to turn off on a heater that is already off', () => {
    /* Otherwise "Off" would be a button that looks live and does nothing — the
       nozzle here sits at target 0. */
    show();

    expect(screen.getAllByText('printers.temperatureControl.turnOff')[0].closest('button')).toBeDisabled();
  });

  it('steps up from off to the floor, which is the first value the machine takes', async () => {
    show({ temperatures: { nozzle: 30, nozzle_target: 0, bed: 25, bed_target: 60 } });

    fireEvent.click(screen.getAllByLabelText('printers.temperatureControl.increase')[0]);

    await waitFor(() => expect(setTemperature).toHaveBeenCalledWith(1, 'nozzle', 20, 0));
  });

  it('never asks for more than the ceiling', async () => {
    show();

    const input = screen.getAllByRole('spinbutton')[1];
    fireEvent.change(input, { target: { value: '9000' } });
    fireEvent.blur(input);

    await waitFor(() => expect(setTemperature).toHaveBeenCalledWith(1, 'bed', 110, 0));
  });

  it('keeps the nozzle usable on a printer that cannot detect a hotend', () => {
    show({ ext_has_nozzle: {} });

    expect(screen.queryByText('printers.temperatureControl.noHotend')).not.toBeInTheDocument();
  });

  it('refuses the nozzle only when the printer explicitly says there is none', () => {
    show({ ext_has_nozzle: { 0: false } });

    expect(screen.getByText('printers.temperatureControl.noHotend')).toBeInTheDocument();
  });

  it('offers no chamber control on a printer that only reads it', () => {
    show({ temperatures: { nozzle: 30, bed: 25, chamber: 28 } });

    expect(screen.getByText('printers.temperatureControl.sensorOnly')).toBeInTheDocument();
  });

  it('controls the chamber where the model actually has a heater', () => {
    show({ temperatures: { nozzle: 30, bed: 25, chamber: 28 } }, { supportsChamberHeater: true });

    expect(screen.queryByText('printers.temperatureControl.sensorOnly')).not.toBeInTheDocument();
    expect(screen.getByText('0–65°C')).toBeInTheDocument();
  });

  it('names both nozzles on a dual-nozzle machine', () => {
    show({ temperatures: { nozzle: 30, nozzle_2: 32, bed: 25 } }, { isDualNozzle: true });

    expect(screen.getByText('printers.temperatureControl.nozzleRight')).toBeInTheDocument();
    expect(screen.getByText('printers.temperatureControl.nozzleLeft')).toBeInTheDocument();
  });

  it('addresses the deputy nozzle by its own index', async () => {
    show({ temperatures: { nozzle: 30, nozzle_2: 32, nozzle_2_target: 200, bed: 25 } }, { isDualNozzle: true });

    fireEvent.click(screen.getAllByLabelText('printers.temperatureControl.increase')[1]);

    await waitFor(() => expect(setTemperature).toHaveBeenCalledWith(1, 'nozzle', 205, 1));
  });

  it('shows no controls at all without the permission', () => {
    show({}, { canControl: false });

    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();
    expect(screen.getAllByText('printers.temperatureControl.noPermission').length).toBeGreaterThan(0);
  });
});
