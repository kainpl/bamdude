/**
 * Provider selection and the OctoEverywhere panel (ported from upstream Bambuddy's tests onto
 * BamDude's write-only credential field: the key is kept, replaced or cleared explicitly).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { FailureDetectionSettings } from '../../components/FailureDetectionSettings';
import { api } from '../../api/client';

const baseSettings = {
  obico_enabled: false, obico_ml_url: '', obico_ml_token: '', obico_sensitivity: 'medium',
  obico_action: 'notify', obico_poll_interval: 10, obico_enabled_printers: '',
  octoeverywhere_enabled: true, octoeverywhere_api_key_configured: true, octoeverywhere_confidence: 'medium',
  octoeverywhere_action: 'notify', octoeverywhere_enabled_printers: '', octoeverywhere_poll_interval: 20,
};

const baseStatus = {
  enabled: true, api_key_configured: true, confidence: 'medium', action: 'notify', poll_interval: 20,
  is_running: true, last_error: null, last_error_code: null, per_printer: {}, history: [],
};

let stored: Record<string, unknown>;

beforeEach(() => {
  vi.restoreAllMocks();
  stored = { ...baseSettings };
  vi.spyOn(api, 'getSettings').mockImplementation(async () => ({ ...stored }) as never);
  vi.spyOn(api, 'getOctoEverywhereStatus').mockResolvedValue(baseStatus as never);
  vi.spyOn(api, 'getObicoStatus').mockResolvedValue({
    is_running: false, last_error: null, per_printer: {}, thresholds: { low: 0.38, high: 0.78 }, history: [], enabled: false,
  } as never);
  vi.spyOn(api, 'getPrinters').mockResolvedValue([{ id: 1, name: 'First' }, { id: 2, name: 'Second' }] as never);
  vi.spyOn(api, 'updateSettings').mockImplementation(async (draft) => {
    stored = { ...stored, ...draft };
    return { ...stored } as never;
  });
});

async function panel() {
  await screen.findByText('OctoEverywhere AI Detection');
  await waitFor(() => expect(screen.getByLabelText('Credential action')).not.toBeDisabled());
}

describe('Failure detection provider', () => {
  it('opens the enabled provider and switches panels by the selector', async () => {
    render(<FailureDetectionSettings />);
    await panel();
    fireEvent.change(screen.getByLabelText('Provider'), { target: { value: 'obico' } });
    expect(await screen.findByText('AI Failure Detection')).toBeInTheDocument();
    expect(screen.queryByText('OctoEverywhere AI Detection')).toBeNull();
  });

  it('opens Obico when OctoEverywhere is off', async () => {
    stored = { ...baseSettings, octoeverywhere_enabled: false };
    render(<FailureDetectionSettings />);
    expect(await screen.findByText('AI Failure Detection')).toBeInTheDocument();
    expect(screen.getByLabelText('Provider')).toHaveValue('obico');
  });
});

describe('OctoEverywhere settings', () => {
  it('tests the saved key by omission, never resending it', async () => {
    const probe = vi.spyOn(api, 'testOctoEverywhereConnection').mockResolvedValue({
      ok: true, status_code: 200, error: null, error_code: null,
    });
    render(<FailureDetectionSettings />);
    await panel();
    fireEvent.click(screen.getByRole('button', { name: /test/i }));
    await waitFor(() => expect(probe).toHaveBeenCalledWith(undefined, 'medium', { octoeverywhere_api_key: 'keep' }));
    expect(await screen.findByText('OctoEverywhere Gadget API key verification successful!')).toBeInTheDocument();
    await act(() => new Promise((resolve) => setTimeout(resolve, 700)));
    expect(api.updateSettings).not.toHaveBeenCalled();
  });

  it('tests a replacement draft without saving it, and saves it only by Save key', async () => {
    const probe = vi.spyOn(api, 'testOctoEverywhereConnection').mockResolvedValue({
      ok: true, status_code: 200, error: null, error_code: null,
    });
    render(<FailureDetectionSettings />);
    await panel();
    fireEvent.change(screen.getByLabelText('Credential action'), { target: { value: 'replace' } });
    const field = await screen.findByLabelText('New credential');
    fireEvent.change(field, { target: { value: 'new-key' } });
    fireEvent.click(screen.getByRole('button', { name: /test/i }));
    await waitFor(() => expect(probe).toHaveBeenCalledWith('new-key', 'medium', { octoeverywhere_api_key: 'replace' }));
    await act(() => new Promise((resolve) => setTimeout(resolve, 700)));
    expect(api.updateSettings).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save key' }));
    await waitFor(() => expect(api.updateSettings).toHaveBeenCalledWith({
      octoeverywhere_api_key: 'new-key', secret_actions: { octoeverywhere_api_key: 'replace' },
    }));
  });

  it('autosaves the confidence and a printer subset; monitor-all is an empty string', async () => {
    render(<FailureDetectionSettings />);
    await panel();
    fireEvent.change(screen.getByLabelText('Confidence'), { target: { value: 'high' } });
    await waitFor(() => expect(api.updateSettings).toHaveBeenCalledWith(
      expect.objectContaining({ octoeverywhere_confidence: 'high', octoeverywhere_enabled_printers: '' }),
    ), { timeout: 3000 });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Monitor all connected printers' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Second' }));
    await waitFor(() => expect(api.updateSettings).toHaveBeenLastCalledWith(
      expect.objectContaining({ octoeverywhere_enabled_printers: '[1]' }),
    ), { timeout: 3000 });
  });

  it('turns OctoEverywhere on with its own switch', async () => {
    stored = { ...baseSettings, octoeverywhere_enabled: false };
    render(<FailureDetectionSettings />);
    fireEvent.change(await screen.findByLabelText('Provider'), { target: { value: 'octoeverywhere' } });
    await panel();
    fireEvent.click(screen.getAllByRole('switch')[0]);
    await waitFor(() => expect(api.updateSettings).toHaveBeenCalledWith(
      expect.objectContaining({ octoeverywhere_enabled: true }),
    ), { timeout: 3000 });
  });

  it('shows the usage-limit notice after a test and in the status', async () => {
    vi.spyOn(api, 'testOctoEverywhereConnection').mockResolvedValue({
      ok: false, status_code: 402, error: 'API usage limit reached. Set up billing to continue.',
      error_code: 'OE_FREE_USAGE_LIMIT_REACHED',
    });
    render(<FailureDetectionSettings />);
    await panel();
    fireEvent.click(screen.getByRole('button', { name: /test/i }));
    expect(await screen.findByText('Usage limit reached.')).toBeInTheDocument();
  });

  it('shows print quality out of ten, and a camera failure apart from a safe verdict', async () => {
    vi.spyOn(api, 'getOctoEverywhereStatus').mockResolvedValue({
      ...baseStatus,
      per_printer: {
        1: { class: 'safe', print_quality: 9, frame_count: 3, error: null, error_code: null },
        2: { class: 'error', print_quality: null, frame_count: 0,
          error: 'Could not capture a camera snapshot. Detection will retry automatically.', error_code: null },
      },
    } as never);
    render(<FailureDetectionSettings />);
    expect(await screen.findByText('Print quality: 9/10')).toBeInTheDocument();
    expect(screen.getByText('AI not checking')).toBeInTheDocument();
    expect(screen.getByText(/Could not capture a camera snapshot/)).toBeInTheDocument();
  });
});
