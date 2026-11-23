import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, discoveryApi, macrosApi, type Printer, type PrinterDiagnosticResult } from '../../api/client';
import { AddPrinterModal, EditPrinterModal } from '../../pages/PrintersPage';
import { render } from '../utils';

const printer = {
  id: 1,
  name: 'X1 Carbon',
  serial_number: '00M09A350100001',
  ip_address: 'printer.local',
  model: 'X1C',
  tags: [],
  is_active: true,
} as unknown as Printer;
const pass: PrinterDiagnosticResult = {
  printer_id: null, ip_address: printer.ip_address, overall: 'ok', checks: [],
};
const fail: PrinterDiagnosticResult = {
  ...pass, overall: 'problems', checks: [{ id: 'mqtt_auth', status: 'fail', params: {} }],
};

describe('printer setup connection check', () => {
  beforeEach(() => {
    vi.spyOn(api, 'diagnoseConnection').mockResolvedValueOnce(fail).mockResolvedValue(pass);
    vi.spyOn(api, 'updatePrinter').mockResolvedValue(printer);
    vi.spyOn(macrosApi, 'getSwapProfiles').mockResolvedValue([]);
    vi.spyOn(discoveryApi, 'getInfo').mockResolvedValue({ is_docker: false, ssdp_running: false, scan_running: false, subnets: [] });
    vi.spyOn(discoveryApi, 'stopDiscovery').mockResolvedValue({ running: false });
    vi.spyOn(discoveryApi, 'stopSubnetScan').mockResolvedValue({ running: false, scanned: 0, total: 0 });
  });
  afterEach(() => vi.restoreAllMocks());

  function open(mode: 'add' | 'edit') {
    const save = vi.fn();
    const view = mode === 'add'
      ? render(<AddPrinterModal onClose={() => {}} onAdd={save} existingSerials={[]} />)
      : render(<EditPrinterModal printer={printer} onClose={() => {}} />);
    const ip = screen.getByPlaceholderText('192.168.1.100 or printer.local');
    const code = screen.getByPlaceholderText(mode === 'add' ? 'From printer settings' : 'Leave empty to keep current');
    const serial = mode === 'add' ? screen.getByPlaceholderText('01P00A000000000') : screen.getByDisplayValue(printer.serial_number);
    if (mode === 'add') {
      fireEvent.change(screen.getByPlaceholderText('My Printer'), { target: { value: printer.name } });
      fireEvent.change(ip, { target: { value: printer.ip_address } });
      fireEvent.change(serial, { target: { value: printer.serial_number } });
      fireEvent.change(code, { target: { value: '12345678' } });
    }
    return { ip, serial, code, save, unmount: view.unmount, submit: () => fireEvent.submit(ip.closest('form')!) };
  }

  it.each([
    ['add', 'ip', 'new-printer.local', 'ip_address'],
    ['add', 'serial', '00M09A350100002', 'serial_number'],
    ['add', 'code', '87654321', 'access_code'],
    ['edit', 'ip', 'new-printer.local', 'ip_address'],
    ['edit', 'code', '87654321', 'access_code'],
  ] as const)('invalidates the %s warning after changing %s', async (mode, field, value, key) => {
    const form = open(mode);
    form.submit();
    await screen.findByRole('button', { name: 'Save anyway' });

    fireEvent.change(form[field], { target: { value } });

    expect(screen.queryByRole('button', { name: 'Save anyway' })).not.toBeInTheDocument();
    form.submit();
    await waitFor(() => expect(api.diagnoseConnection).toHaveBeenNthCalledWith(2, expect.objectContaining({ [key]: value })));
    if (mode === 'add') {
      await waitFor(() => expect(form.save).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ [key]: value })));
    } else {
      expect(form.serial).toBeDisabled();
      await waitFor(() => expect(api.updatePrinter).toHaveBeenCalledExactlyOnceWith(1, expect.objectContaining({
        [key]: value,
      })));
    }
  });

  it.each(['add', 'edit'] as const)('checks again in %s with the same settings', async (mode) => {
    const form = open(mode);
    form.submit();
    await screen.findByRole('button', { name: 'Save anyway' });
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));

    await waitFor(() => expect(api.diagnoseConnection).toHaveBeenCalledTimes(2));
    const diagnostic = vi.mocked(api.diagnoseConnection);
    expect(diagnostic.mock.calls[1]).toEqual(diagnostic.mock.calls[0]);
    if (mode === 'edit') {
      await waitFor(() => expect(api.updatePrinter).toHaveBeenCalledOnce());
      expect(vi.mocked(api.updatePrinter).mock.calls[0][1]).not.toHaveProperty('access_code');
    } else {
      await waitFor(() => expect(form.save).toHaveBeenCalledOnce());
    }
  });

  it.each(['add', 'edit'] as const)('ignores a late successful %s check after the address was edited and reverted', async (mode) => {
    let finish!: (result: PrinterDiagnosticResult) => void;
    vi.mocked(api.diagnoseConnection).mockReset()
      .mockReturnValueOnce(new Promise(resolve => { finish = resolve; }))
      .mockResolvedValueOnce(fail);
    const form = open(mode);
    form.submit();
    fireEvent.change(form.ip, { target: { value: 'other.local' } });
    fireEvent.change(form.ip, { target: { value: printer.ip_address } });
    form.submit();
    await screen.findByRole('button', { name: 'Save anyway' });
    await act(async () => finish(pass));

    expect(api.updatePrinter).not.toHaveBeenCalled();
    expect(form.save).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Save anyway' })).toBeInTheDocument();
  });

  it('never saves a dismissed add dialog when a check returns', async () => {
    let finish!: (result: PrinterDiagnosticResult) => void;
    vi.mocked(api.diagnoseConnection).mockReset().mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const form = open('add');
    form.submit();
    form.unmount();
    await act(async () => finish(pass));
    expect(form.save).not.toHaveBeenCalled();
  });

  it.each(['add', 'edit'] as const)('reveals only the entered %s code on request', async (mode) => {
    const { code } = open(mode);
    await act(async () => {});
    expect(code).toHaveAttribute('type', 'password');
    if (mode === 'edit') expect(code).toHaveValue('');
    const reveal = screen.getByRole('button', { name: 'Show access code' });
    expect(reveal).not.toHaveAttribute('tabindex', '-1');
    fireEvent.click(reveal);
    expect(code).toHaveAttribute('type', 'text');
    fireEvent.click(screen.getByRole('button', { name: 'Hide access code' }));
    expect(code).toHaveAttribute('type', 'password');
  });

  it('blocks Edit retries and form submits while Save anyway is saving, then allows retry after failure', async () => {
    let rejectSave!: (error: Error) => void;
    vi.mocked(api.updatePrinter).mockReset()
      .mockReturnValueOnce(new Promise((_, reject) => { rejectSave = reject; }))
      .mockResolvedValueOnce(printer);
    const form = open('edit');
    form.submit();
    const saveAnyway = await screen.findByRole('button', { name: 'Save anyway' });
    fireEvent.click(saveAnyway);
    await waitFor(() => expect(api.updatePrinter).toHaveBeenCalledOnce());

    const name = form.ip.closest('form')!.querySelector('input[value="X1 Carbon"]')!;
    fireEvent.change(name, { target: { value: 'Renamed during save' } });
    const checkAgain = screen.getByRole('button', { name: 'Check again' });
    fireEvent.click(checkAgain);
    await act(async () => { form.submit(); });
    expect(api.diagnoseConnection).toHaveBeenCalledOnce();
    expect(api.updatePrinter).toHaveBeenCalledExactlyOnceWith(1, expect.objectContaining({ name: printer.name }));
    expect(saveAnyway).toBeDisabled();
    expect(checkAgain).toBeDisabled();

    await act(async () => rejectSave(new Error('Could not save printer')));
    await waitFor(() => expect(checkAgain).toBeEnabled());
    expect(saveAnyway).toBeEnabled();
    fireEvent.click(checkAgain);
    await waitFor(() => expect(api.diagnoseConnection).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(api.updatePrinter).toHaveBeenCalledTimes(2));
    expect(api.updatePrinter).toHaveBeenNthCalledWith(2, 1, expect.objectContaining({ name: 'Renamed during save' }));
  });

  it('keeps Save anyway available for the checked settings', async () => {
    const form = open('add');
    form.submit();
    fireEvent.click(await screen.findByRole('button', { name: 'Save anyway' }));
    expect(form.save).toHaveBeenCalledOnce();
    expect(api.diagnoseConnection).toHaveBeenCalledOnce();
  });
});
