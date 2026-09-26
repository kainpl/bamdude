/**
 * Test Connection checks the configuration the detection loop runs with
 * (upstream 06e5114a, #2952): the loop reads the SAVED settings, and inside the
 * auto-save debounce the boxes hold something else — a green "healthy" for a
 * token the service never received is the reassuring-green-light problem again.
 * So an unsaved form is saved before the probe.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
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

  it('saves a form that changed before probing it', async () => {
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
    expect(calls[0]).toBe('save');
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
});
