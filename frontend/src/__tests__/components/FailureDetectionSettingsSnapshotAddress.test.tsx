import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { FailureDetectionSettings } from '../../components/FailureDetectionSettings';
import { api } from '../../api/client';

describe('FailureDetectionSettings — the address for the ML API and the pause level', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getSettings').mockResolvedValue({
      obico_enabled: true, obico_ml_url: 'http://ml:3333', obico_snapshot_base_url: 'http://old:8000', obico_ml_token: '',
      obico_sensitivity: 'medium', obico_action: 'pause', obico_poll_interval: 10, obico_enabled_printers: '',
    } as never);
    vi.spyOn(api, 'getObicoStatus').mockResolvedValue({
      is_running: true, last_error: null, per_printer: {}, history: [], enabled: true, external_url_configured: true,
      thresholds: { low: 0.38, high: 0.78, pause: 1.365 },
    } as never);
    vi.spyOn(api, 'getPrinters').mockResolvedValue([]);
  });

  it('is saved with the connection, trimmed, and only when it changed', async () => {
    const save = vi.spyOn(api, 'updateSettings').mockResolvedValue({ obico_ml_url: 'http://ml:3333', obico_snapshot_base_url: 'http://bamdude:8000' } as never);
    render(<FailureDetectionSettings />);
    const field = await screen.findByLabelText('BamDude address for the ML API (optional)');
    await waitFor(() => expect((field as HTMLInputElement).value).toBe('http://old:8000'));
    const button = screen.getByRole('button', { name: 'Save connection' });
    expect(button).toBeDisabled();

    fireEvent.change(field, { target: { value: '  http://bamdude:8000 ' } });
    expect(button).toBeEnabled();
    fireEvent.click(button);

    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(save.mock.calls[0][0]).toMatchObject({ obico_ml_url: 'http://ml:3333', obico_snapshot_base_url: 'http://bamdude:8000' });
  });

  it('shows the level a pause waits for beside the alert thresholds', async () => {
    render(<FailureDetectionSettings />);
    expect(await screen.findByText('Pause level')).toBeInTheDocument();
    expect(screen.getByText('1.36')).toBeInTheDocument();
    expect(screen.getByText('0.38 / 0.78')).toBeInTheDocument();
  });
});
