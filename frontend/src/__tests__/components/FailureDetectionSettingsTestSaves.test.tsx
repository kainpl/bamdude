/**
 * Unit B probes a locked draft without turning Test into a persistence action.
 * The sender resolves keep/clear/replace against the same destination contract.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { FailureDetectionSettings } from '../../components/FailureDetectionSettings';
import { api } from '../../api/client';

describe('FailureDetectionSettings — Test Connection', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getSettings').mockResolvedValue({
      obico_enabled: true, obico_ml_url: 'http://old:3333', obico_ml_token: '', obico_sensitivity: 'medium',
      obico_action: 'notify', obico_poll_interval: 10, obico_enabled_printers: '',
    } as never);
    vi.spyOn(api, 'getObicoStatus').mockResolvedValue({
      is_running: true, last_error: null, per_printer: {}, thresholds: { low: 0.38, high: 0.78 }, history: [], enabled: true,
    } as never);
    vi.spyOn(api, 'getPrinters').mockResolvedValue([]);
  });

  it('probes the edited draft without saving it first', async () => {
    const calls: string[] = [];
    vi.spyOn(api, 'updateSettings').mockImplementation(async () => { calls.push('save'); return {} as never; });
    vi.spyOn(api, 'testObicoConnection').mockImplementation(async () => {
      calls.push('probe');
      return { ok: true, status_code: 200, body: null, error: null } as never;
    });
    render(<FailureDetectionSettings />);
    const url = await screen.findByPlaceholderText('http://192.168.1.10:3333');
    await waitFor(() => expect((url as HTMLInputElement).value).toBe('http://old:3333'));
    fireEvent.change(url, { target: { value: 'http://new:3333' } });
    fireEvent.click(screen.getByRole('button', { name: /test/i }));
    await waitFor(() => expect(calls).toContain('probe'));
    await act(() => new Promise(resolve => setTimeout(resolve, 750)));
    expect(calls).toEqual(['probe']);
    expect(api.testObicoConnection).toHaveBeenCalledWith('http://new:3333', undefined, {});
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() => expect(calls).toEqual(['probe', 'save']));
  });

  it('lists a watched print with no verdict as not checking, with its reason and no score', async () => {
    vi.spyOn(api, 'getObicoStatus').mockResolvedValue({
      is_running: true, last_error: null, thresholds: { low: 0.38, high: 0.78 }, history: [], enabled: true,
      per_printer: { 1: { class: 'error', frame_count: 0, score: 0, error: 'Obico ML API rejected the token.' } },
    } as never);
    render(<FailureDetectionSettings />);
    const cell = await screen.findByText('not checking');
    expect(cell).toHaveAttribute('title', 'Obico ML API rejected the token.');
    expect(screen.queryByText(/0\.000/)).toBeNull();
  });

  it('tests opaque saved URLs and saved tokens by omission without saving a reconstructed URL', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue({
      obico_enabled: true, obico_ml_url: '', obico_ml_url_opaque: true, obico_ml_url_configured: true,
      obico_ml_token_configured: true, obico_sensitivity: 'medium', obico_action: 'notify',
      obico_poll_interval: 10, obico_enabled_printers: '',
    } as never);
    const save = vi.spyOn(api, 'updateSettings').mockResolvedValue({} as never);
    const probe = vi.spyOn(api, 'testObicoConnection').mockResolvedValue({ ok: true } as never);
    render(<FailureDetectionSettings />);
    await waitFor(() => expect(screen.getAllByLabelText('Credential action')).toHaveLength(2));
    fireEvent.click(screen.getByRole('button', { name: /test/i }));
    await waitFor(() => expect(probe).toHaveBeenCalledWith(undefined, undefined,
      { obico_ml_url: 'keep', obico_ml_token: 'keep' }));
    await act(() => new Promise(resolve => setTimeout(resolve, 750)));
    expect(save).not.toHaveBeenCalled();
  });

  it('autosaves an ordinary preference using the committed connection and retains the unsaved connection draft', async () => {
    let stored: Record<string, unknown> = { obico_enabled: true, obico_ml_url: 'http://old:3333',
      obico_ml_token_configured: true, obico_save_detections: false, obico_sensitivity: 'medium',
      obico_action: 'notify', obico_poll_interval: 10, obico_enabled_printers: '' };
    vi.spyOn(api, 'getSettings').mockImplementation(async () => stored as never);
    const save = vi.spyOn(api, 'updateSettings').mockImplementation(async draft => {
      stored = { ...stored, ...draft };
      return stored as never;
    });
    const probe = vi.spyOn(api, 'testObicoConnection').mockResolvedValue({ ok: true } as never);
    render(<FailureDetectionSettings />);
    const url = await screen.findByPlaceholderText('http://192.168.1.10:3333');
    await waitFor(() => expect(url).toHaveValue('http://old:3333'));
    fireEvent.change(url, { target: { value: 'http://draft:3333' } });
    fireEvent.change(screen.getByLabelText('Credential action'), { target: { value: 'replace' } });
    fireEvent.change(screen.getByLabelText('New credential'), { target: { value: 'unsaved-canary' } });
    fireEvent.click(screen.getByRole('switch', { name: 'Save detection frames' }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    await act(() => new Promise(resolve => setTimeout(resolve, 750)));
    expect(save.mock.calls[0][0]).not.toHaveProperty('obico_ml_url');
    expect(save.mock.calls[0][0]).not.toHaveProperty('obico_ml_token');
    expect(url).toHaveValue('http://draft:3333');
    expect(screen.getByLabelText('New credential')).toHaveValue('unsaved-canary');
    fireEvent.click(screen.getByRole('button', { name: /test/i }));
    await waitFor(() => expect(probe).toHaveBeenCalledWith('http://draft:3333', 'unsaved-canary', { obico_ml_token: 'replace' }));
    await act(() => new Promise(resolve => setTimeout(resolve, 750)));
    expect(save).toHaveBeenCalledOnce();
  });
});
