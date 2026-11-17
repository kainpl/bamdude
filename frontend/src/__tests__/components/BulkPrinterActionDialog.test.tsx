import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import i18n from '../../i18n';
import { api, type Printer, type PrinterStatus, type MaintenanceStatus, type PrinterQueue, type WaitingPrint } from '../../api/client';
import { BulkPrinterActionDialog } from '../../components/BulkPrinterActionDialog';
import { BulkPrinterToolbar, type BulkAction } from '../../components/BulkPrinterToolbar';

const { allowed } = vi.hoisted(() => ({ allowed: { value: true } }));
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ hasPermission: () => allowed.value }) }));

const printers = [1, 2].map(id => ({ id, name: `Product ${id === 1 ? 'A' : 'B'} printer`, model: 'P1S', is_active: true, archived: false })) as Printer[];
const idle = { connected: true, state: 'IDLE', hms_errors: [], awaiting_plate_clear: false } as unknown as PrinterStatus;
const options = { bed_leveling: true, vibration: true, motor_noise: true, nozzle_offset: false, high_temp_heatbed: false, lidar: false, clump_pos: false };
const task = (id: number, printerId: number, type: number, name: string): MaintenanceStatus => ({ id, printer_id: printerId, maintenance_type_id: type, maintenance_type_name: name, enabled: true } as MaintenanceStatus);

function mount(action: BulkAction, selection = printers) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  const close = vi.fn();
  const view = render(<QueryClientProvider client={client}><BulkPrinterActionDialog action={action} printers={selection} onClose={close} /></QueryClientProvider>);
  return { ...view, close, client };
}
async function confirmButton() {
  return screen.findByRole('button', { name: /^Confirm:/ });
}

beforeEach(async () => {
  vi.restoreAllMocks();
  allowed.value = true;
  await i18n.changeLanguage('en');
  vi.spyOn(api, 'getPrinterStatus').mockResolvedValue(idle);
  vi.spyOn(api, 'getPrinter').mockImplementation(async id => printers.find(p => p.id === id)!);
  vi.spyOn(api, 'getCalibrationOptions').mockResolvedValue(options);
  vi.spyOn(api, 'getWaitingPrint').mockResolvedValue({ archive_id: 100, gate_token: 'gate-A' } as WaitingPrint);
  vi.spyOn(api, 'getQueues').mockResolvedValue([1, 2].map(id => ({ id: id + 10, printer_id: id, is_paused: false })) as PrinterQueue[]);
  vi.spyOn(api, 'getPrinterMaintenance').mockImplementation(async id => ({ printer_id: id, maintenance_items: [task(id * 10, id, 1, 'Lubricate guides'), task(id * 10 + 1, id, 2, 'Clean plate')] } as Awaited<ReturnType<typeof api.getPrinterMaintenance>>));
  vi.spyOn(api, 'homeAxes').mockResolvedValue({ success: true, message: 'sent' });
  vi.spyOn(api, 'startCalibration').mockResolvedValue({ success: true });
  vi.spyOn(api, 'setChamberLight').mockResolvedValue({ success: true, message: 'sent' });
  vi.spyOn(api, 'stopPrint').mockResolvedValue({ success: true, message: 'sent' });
  vi.spyOn(api, 'pausePrint').mockResolvedValue({ success: true, message: 'sent' });
  vi.spyOn(api, 'resumePrint').mockResolvedValue({ success: true, message: 'sent' });
  vi.spyOn(api, 'clearHMSErrors').mockResolvedValue({ success: true, message: 'sent' });
  vi.spyOn(api, 'clearPlate').mockResolvedValue({ success: true, message: 'recorded' });
  vi.spyOn(api, 'performMaintenance').mockResolvedValue(task(10, 1, 1, 'Lubricate guides'));
  vi.spyOn(api, 'updateQueue').mockResolvedValue({} as PrinterQueue);
  vi.spyOn(api, 'updatePrinter').mockResolvedValue(printers[0]);
});

describe('confirmed batch printer actions', () => {
  it('home cannot move on open, cancellation, or without explicit empty-plate confirmation', async () => {
    const user = userEvent.setup();
    const { close } = mount('home');
    const button = await confirmButton();
    await screen.findByText('Product A printer');
    expect(button).toBeDisabled();
    expect(api.homeAxes).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(close).toHaveBeenCalledOnce();
    expect(api.homeAxes).not.toHaveBeenCalled();
  });

  it('names both recipients and skips a printer that starts preparing after confirmation', async () => {
    const user = userEvent.setup();
    let secondReads = 0;
    vi.mocked(api.getPrinterStatus).mockImplementation(async id => id === 2 && ++secondReads > 1 ? { ...idle, state: 'PREPARE' } : idle);
    mount('home');
    await screen.findByText('Product B printer');
    await user.click(screen.getByRole('checkbox', { name: /plates/ }));
    await user.click(await confirmButton());
    await screen.findByText('Results: Home axes…');
    expect(api.homeAxes).toHaveBeenCalledExactlyOnceWith(1, 'all');
    expect(screen.getByText(/Printer is busy/)).toBeInTheDocument();
  });

  it('does not retry refused publication and reports partial success', async () => {
    const user = userEvent.setup();
    vi.mocked(api.setChamberLight).mockImplementation(async id => ({ success: id === 1, message: '' }));
    mount('lightOn');
    await waitFor(async () => expect(await confirmButton()).toBeEnabled());
    expect(api.setChamberLight).not.toHaveBeenCalled();
    await user.dblClick(await confirmButton());
    await screen.findByText('Results: Turn lights on…');
    expect(api.setChamberLight).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/Command was refused/)).toBeInTheDocument();
    expect(screen.getByText('Command sent')).toBeInTheDocument();
  });

  it('filters offline and unknown-state printers before homing', async () => {
    vi.mocked(api.getPrinterStatus).mockImplementation(async id => id === 1 ? { ...idle, connected: false } : { ...idle, state: 'unknown' });
    mount('home');
    await screen.findByText('Printer is offline');
    expect(screen.getByText('No known idle state')).toBeInTheDocument();
    expect(await confirmButton()).toBeDisabled();
    expect(api.homeAxes).not.toHaveBeenCalled();
  });

  it('calibrates only printers supporting every selected option', async () => {
    const user = userEvent.setup();
    vi.mocked(api.getCalibrationOptions).mockImplementation(async id => ({ ...options, nozzle_offset: id === 1 }));
    mount('calibrate');
    await user.click(await screen.findByRole('checkbox', { name: /Nozzle.*offset/i }));
    await user.click(screen.getByRole('checkbox', { name: /plates/ }));
    expect(api.startCalibration).not.toHaveBeenCalled();
    await user.click(await confirmButton());
    await screen.findByText('Results: Calibrate printers…');
    expect(api.startCalibration).toHaveBeenCalledExactlyOnceWith(1, { nozzle_offset: true });
  });

  it('records only selected maintenance items with the common note, preserving hours and other intervals', async () => {
    const user = userEvent.setup();
    const setHours = vi.spyOn(api, 'setPrinterHours');
    mount('maintenance');
    await user.click(await screen.findByRole('checkbox', { name: 'Lubricate guides' }));
    await user.type(screen.getByRole('textbox', { name: /Common note/ }), 'Done together');
    expect(api.performMaintenance).not.toHaveBeenCalled();
    await user.click(await confirmButton());
    await screen.findByText('Results: Record maintenance…');
    expect(vi.mocked(api.performMaintenance).mock.calls).toEqual([[10, 'Done together'], [20, 'Done together']]);
    expect(setHours).not.toHaveBeenCalled();
  });

  it('does not apply newly assigned maintenance items that were not in the confirmation', async () => {
    const user = userEvent.setup();
    let reads = 0;
    vi.mocked(api.getPrinterMaintenance).mockImplementation(async id => ({ maintenance_items: [task(++reads === 1 ? 10 : 99, id, 1, 'Lubricate guides')] } as Awaited<ReturnType<typeof api.getPrinterMaintenance>>));
    mount('maintenance', [printers[0]]);
    await user.click(await screen.findByRole('checkbox', { name: 'Lubricate guides' }));
    await user.click(await confirmButton());
    await screen.findByText('Results: Record maintenance…');
    expect(api.performMaintenance).not.toHaveBeenCalled();
    expect(screen.getByText(/State changed/)).toBeInTheDocument();
  });

  it('pauses future queue dispatch without pausing the physical print', async () => {
    const user = userEvent.setup();
    mount('queuePause');
    await waitFor(async () => expect(await confirmButton()).toBeEnabled());
    await user.click(await confirmButton());
    await screen.findByText('Results: Pause queues…');
    expect(vi.mocked(api.updateQueue).mock.calls).toEqual([[11, { is_paused: true }], [12, { is_paused: true }]]);
    expect(api.pausePrint).not.toHaveBeenCalled();
  });

  it('does not enable maintenance mode on a busy printer', async () => {
    vi.mocked(api.getPrinterStatus).mockResolvedValue({ ...idle, state: 'RUNNING' });
    mount('serviceOn');
    await screen.findAllByText('Printer is busy');
    expect(await confirmButton()).toBeDisabled();
    expect(api.updatePrinter).not.toHaveBeenCalled();
  });

  it('never clears a newer plate-clear gate than the one shown', async () => {
    const user = userEvent.setup();
    vi.mocked(api.getPrinterStatus).mockResolvedValue({ ...idle, awaiting_plate_clear: true, state: 'FINISH' });
    vi.mocked(api.getWaitingPrint).mockResolvedValueOnce({ archive_id: 100, gate_token: 'old' } as WaitingPrint).mockResolvedValue({ archive_id: 101, gate_token: 'new' } as WaitingPrint);
    mount('clearPlate', [printers[0]]);
    await waitFor(async () => expect(await confirmButton()).toBeEnabled());
    await user.click(await confirmButton());
    await screen.findByText('Results: Clear Plate');
    expect(api.clearPlate).not.toHaveBeenCalled();
  });

  it('refuses commands without permission even when the dialog is opened directly', async () => {
    allowed.value = false;
    mount('lightOff');
    await screen.findAllByText('Insufficient permission');
    expect(await confirmButton()).toBeDisabled();
    expect(api.setChamberLight).not.toHaveBeenCalled();
  });

  it.each(['stop', 'pause', 'resume', 'clearHMS', 'clearPlate', 'queueResume', 'serviceOff', 'lightOff'] as BulkAction[])('%s opening never performs a write', async action => {
    mount(action);
    await screen.findByText('Product A printer');
    for (const method of [api.stopPrint, api.pausePrint, api.resumePrint, api.clearHMSErrors, api.clearPlate, api.updateQueue, api.updatePrinter, api.setChamberLight]) expect(method).not.toHaveBeenCalled();
  });

  it('the existing toolbar menu opens a confirmation intent, not a device command', async () => {
    const user = userEvent.setup();
    const client = new QueryClient();
    const onAction = vi.fn();
    render(<QueryClientProvider client={client}><BulkPrinterToolbar selectedIds={new Set([1, 2])} printers={printers} onClose={vi.fn()} onSelectAll={vi.fn()} onSelectByLocation={vi.fn()} onSelectByState={vi.fn()} onAction={onAction} onPlacement={vi.fn()} actionPending={false} /></QueryClientProvider>);
    await user.click(screen.getByRole('button', { name: 'Actions' }));
    await user.click(screen.getByRole('menuitem', { name: 'Home axes…' }));
    expect(onAction).toHaveBeenCalledExactlyOnceWith('home');
    expect(api.homeAxes).not.toHaveBeenCalled();
  });

  it.each(['stop', 'pause', 'resume', 'clearHMS', 'clearPlate', 'queueResume', 'serviceOn', 'serviceOff', 'lightOff'] as const)('%s writes only after confirmation with the intended arguments', async action => {
    const user = userEvent.setup();
    vi.mocked(api.getPrinterStatus).mockResolvedValue({ ...idle,
      state: action === 'stop' || action === 'pause' ? 'RUNNING' : action === 'resume' ? 'PAUSE' : action === 'clearPlate' ? 'FINISH' : 'IDLE',
      awaiting_plate_clear: action === 'clearPlate',
      hms_errors: action === 'clearHMS' ? [{ code: '123', attr: 0, module: 1, severity: 3 }] : [],
    });
    if (action === 'queueResume') vi.mocked(api.getQueues).mockResolvedValue([{ id: 11, printer_id: 1, is_paused: true }] as PrinterQueue[]);
    if (action === 'serviceOff') vi.mocked(api.getPrinter).mockResolvedValue({ ...printers[0], is_active: false });
    mount(action, [printers[0]]);
    await waitFor(async () => expect(await confirmButton()).toBeEnabled());
    const method = action === 'stop' ? api.stopPrint : action === 'pause' ? api.pausePrint : action === 'resume' ? api.resumePrint
      : action === 'clearHMS' ? api.clearHMSErrors : action === 'clearPlate' ? api.clearPlate : action === 'queueResume' ? api.updateQueue
      : action === 'lightOff' ? api.setChamberLight : api.updatePrinter;
    expect(method).not.toHaveBeenCalled();
    await user.click(await confirmButton());
    await screen.findByText(/^Results:/);
    if (action === 'queueResume') expect(api.updateQueue).toHaveBeenCalledExactlyOnceWith(11, { is_paused: false });
    else if (action === 'serviceOn' || action === 'serviceOff') expect(api.updatePrinter).toHaveBeenCalledExactlyOnceWith(1, { is_active: action === 'serviceOff' });
    else if (action === 'clearPlate') expect(api.clearPlate).toHaveBeenCalledExactlyOnceWith(1, { expected_archive_id: 100, expected_gate_token: 'gate-A' });
    else if (action === 'lightOff') expect(api.setChamberLight).toHaveBeenCalledExactlyOnceWith(1, false);
    else expect(method).toHaveBeenCalledExactlyOnceWith(1);
  });

  it('a changed queue is skipped rather than mutating a new queue or overriding another operator', async () => {
    const user = userEvent.setup();
    vi.mocked(api.getQueues).mockResolvedValueOnce([{ id: 11, printer_id: 1, is_paused: false }] as PrinterQueue[]).mockResolvedValue([{ id: 12, printer_id: 1, is_paused: false }] as PrinterQueue[]);
    mount('queuePause', [printers[0]]);
    await waitFor(async () => expect(await confirmButton()).toBeEnabled());
    await user.click(await confirmButton());
    await screen.findByText('Results: Pause queues…');
    expect(api.updateQueue).not.toHaveBeenCalled();
  });

  it('maintenance failure remains visible alongside successful records', async () => {
    const user = userEvent.setup();
    vi.mocked(api.performMaintenance).mockImplementation(async id => {
      if (id === 20) throw new Error('Synthetic storage refusal');
      return task(10, 1, 1, 'Lubricate guides');
    });
    mount('maintenance');
    await user.click(await screen.findByRole('checkbox', { name: 'Lubricate guides' }));
    await user.click(await confirmButton());
    await screen.findByText('Results: Record maintenance…');
    expect(screen.getByText('Recorded')).toBeInTheDocument();
    expect(screen.getByText(/Synthetic storage refusal/)).toBeInTheDocument();
    expect(api.performMaintenance).toHaveBeenCalledTimes(2);
  });
});
