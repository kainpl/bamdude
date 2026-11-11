import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useQuery } from '@tanstack/react-query';
import { render } from '../utils';
import { api } from '../../api/client';
import { MQTTDebugModal } from '../../components/MQTTDebugModal';

afterEach(() => vi.restoreAllMocks());

it('refreshes the card after start and stop even without WebSocket traffic', async () => {
  let recording = false;
  vi.spyOn(api, 'getMQTTRecording').mockImplementation(async () => ({ entries: [], recording, size_bytes: 128 }));
  vi.spyOn(api, 'setMQTTRecording').mockImplementation(async (_id, enabled) => {
    recording = enabled;
    return { enabled, file: 'synthetic.log' };
  });
  function CardStatus() {
    const { data } = useQuery({
      queryKey: ['printerStatus', 7],
      queryFn: async () => ({ mqtt_recording: recording }),
      staleTime: Infinity,
    });
    return <span data-testid="card-recording">{String(data?.mqtt_recording)}</span>;
  }
  render(<><CardStatus /><MQTTDebugModal printerId={7} printerName="Synthetic" onClose={vi.fn()} /></>);
  await waitFor(() => expect(screen.getByTestId('card-recording')).toHaveTextContent('false'));
  await userEvent.click(await screen.findByRole('button', { name: /start logging/i }));
  await waitFor(() => expect(screen.getByTestId('card-recording')).toHaveTextContent('true'));
  await userEvent.click(await screen.findByRole('button', { name: /stop logging/i }));
  await waitFor(() => expect(screen.getByTestId('card-recording')).toHaveTextContent('false'));
});

function mount(size = 128) {
  vi.spyOn(api, 'getMQTTRecording').mockResolvedValue({ entries: [], recording: false, size_bytes: size });
  render(<MQTTDebugModal printerId={7} printerName="Synthetic Printer" onClose={vi.fn()} />);
}

describe('MQTT recording download button', () => {
  it('disables repeat downloads until the authenticated download finishes', async () => {
    let finish!: () => void;
    const download = vi.spyOn(api, 'downloadMQTTRecording').mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
    mount();
    const button = screen.getByRole('button', { name: /download/i });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    expect(download).toHaveBeenCalledWith(7);
    expect(button).toBeDisabled();
    finish();
    await waitFor(() => expect(button).toBeEnabled());
  });

  it('shows a failed download and allows retry', async () => {
    vi.spyOn(api, 'downloadMQTTRecording').mockRejectedValue(new Error('Recording no longer exists'));
    mount();
    const button = screen.getByRole('button', { name: /download/i });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    expect(await screen.findByText('Recording no longer exists')).toBeVisible();
    expect(button).toBeEnabled();
  });

  it('does not offer downloading an empty recording', async () => {
    mount(0);
    expect(screen.getByRole('button', { name: /download/i })).toBeDisabled();
  });
});
