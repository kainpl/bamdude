import { describe, expect, it } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { StandaloneCamerasPanel } from '../../components/settings/StandaloneCamerasPanel';

const row = { id: 17, name: 'Private shelf', camera_type: 'rtsp', enabled: true, rotation: 0, location_id: null, location_name: null,
  url_configured: true, snapshot_url_configured: true, source_host: '192.0.2.71',
  url: 'rtsp://user:LEGACY-BA4-CANARY@192.0.2.71/live', snapshot_url: 'http://192.0.2.72/LEGACY-SNAPSHOT-CANARY' };
function serve(gate?: Promise<void>) {
  const saves: Record<string, unknown>[] = [];
  const probes: Record<string, unknown>[] = [];
  server.use(
    http.get('*/api/v1/cameras/', () => HttpResponse.json([row])),
    http.patch('*/api/v1/cameras/17', async ({ request }) => {
      saves.push(await request.json() as Record<string, unknown>);
      await gate;
      return HttpResponse.json({ ...row, url: undefined, snapshot_url: undefined });
    }),
    http.post('*/api/v1/cameras/17/test', async ({ request }) => {
      probes.push(await request.json() as Record<string, unknown>);
      return HttpResponse.json({ success: true });
    }),
  );
  return { saves, probes };
}
async function pause() { await act(() => new Promise(resolve => setTimeout(resolve, 750))); }

describe('Standalone camera opaque edits', () => {
  it('keeps both URLs opaque while renaming through an explicit save', async () => {
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    const { saves } = serve(gate); render(<StandaloneCamerasPanel />);
    await screen.findByText('Private shelf');
    expect(document.getElementById('camera-17-url')).toHaveValue('');
    expect(document.getElementById('camera-17-snapshot')).toHaveValue('');
    expect(document.body.innerHTML).not.toContain('CANARY');
    fireEvent.change(document.getElementById('camera-17-name')!, { target: { value: 'Renamed shelf' } });
    await pause(); expect(saves).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(saves).toHaveLength(1));
    expect(saves[0]).toMatchObject({ name: 'Renamed shelf', secret_actions: { url: 'keep', snapshot_url: 'keep' } });
    expect(saves[0]).not.toHaveProperty('url'); expect(saves[0]).not.toHaveProperty('snapshot_url');
    await waitFor(() => expect(document.querySelector('fieldset')).toBeDisabled());
    expect(document.getElementById('camera-17-name')).toBeDisabled();
    await act(async () => { release(); });
    await waitFor(() => expect(document.querySelector('fieldset')).not.toBeDisabled());
  });

  it('tests a complete URL draft without delayed persistence, then saves once', async () => {
    const { saves, probes } = serve(); render(<StandaloneCamerasPanel />);
    await screen.findByText('Private shelf');
    const input = document.getElementById('camera-17-url')!;
    fireEvent.change(input, { target: { value: 'rtsp://draft.invalid/partial' } });
    await pause(); expect(saves).toEqual([]);
    const replacement = 'rtsp://newuser:FULL-BA4-CREDENTIAL@192.0.2.73/live';
    fireEvent.change(input, { target: { value: replacement } });
    fireEvent.click(screen.getAllByRole('button', { name: /^test$/i })[0]);
    await waitFor(() => expect(probes).toHaveLength(1));
    await pause(); expect(saves).toEqual([]);
    expect(probes[0]).toMatchObject({ url: replacement, camera_type: 'rtsp', secret_actions: { url: 'replace', snapshot_url: 'keep' } });
    expect(probes[0]).not.toHaveProperty('snapshot_url');
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(saves).toHaveLength(1));
    expect(saves[0]).toMatchObject({ url: replacement, secret_actions: { url: 'replace' } });
  });

  it('tests stored sources through their addressed editor endpoint and clears only on Save', async () => {
    const { saves, probes } = serve(); render(<StandaloneCamerasPanel />);
    await screen.findByText('Private shelf');
    fireEvent.click(screen.getAllByRole('button', { name: /^test$/i })[0]);
    await waitFor(() => expect(probes).toHaveLength(1));
    expect(probes[0]).not.toHaveProperty('url'); expect(probes[0]).not.toHaveProperty('snapshot_url');
    fireEvent.click(screen.getAllByRole('button', { name: /Clear credential:/i })[0]);
    await pause(); expect(saves).toEqual([]);
    expect(screen.getAllByRole('button', { name: /^test$/i })[0]).toBeDisabled();
    fireEvent.click(screen.getAllByRole('button', { name: /^test$/i })[1]);
    await waitFor(() => expect(probes).toHaveLength(2));
    expect(probes[1]).toMatchObject({ source: 'snapshot', enabled: false, secret_actions: { url: 'clear', snapshot_url: 'keep' } });
    expect(probes[1]).not.toHaveProperty('url'); expect(probes[1]).not.toHaveProperty('snapshot_url');
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(saves).toHaveLength(1));
    expect(saves[0]).toMatchObject({ enabled: false, secret_actions: { url: 'clear' } });
    expect(saves[0]).not.toHaveProperty('url');
  });
});
