import { describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { AutoModeOptions } from '../../components/PrintModal/AutoModeOptions';
import { DEFAULT_AUTO_MODE_OPTIONS } from '../../components/PrintModal/types';
import type { RoutingPreview } from '../../api/client';

const preview: RoutingPreview = { plates: [{
  requested_plate_id: 15, plate_id: 15, model: 'P1P', status: 'ok', reason: null,
  filaments: [{ slot_id: 3, type: 'PETG', color: '#000000', nozzle_id: null, used_grams: 0.0001 }],
  groups: [
    { key: 'without', model: 'P1P', nozzles: 1, ams: 'absent', total: 5, compatible: 5, ready: 2, incompatible: 0, unknown: 0, reasons: [] },
    { key: 'with', model: 'P1P', nozzles: 1, ams: 'present', total: 2, compatible: 0, ready: 0, incompatible: 2, unknown: 0,
      reasons: [{ code: 'material_mismatch', message: 'No PETG available', count: 2 }] },
  ],
}] };

describe('AutoQueue routing options', () => {
  it('shows separate hardware groups and compatibility versus readiness', () => {
    render(<AutoModeOptions options={DEFAULT_AUTO_MODE_OPTIONS} onChange={vi.fn()} printers={[]} preview={preview} />);
    expect(screen.getByText(/Without AMS/)).toBeInTheDocument();
    expect(screen.getByText(/AMS connected/)).toBeInTheDocument();
    expect(screen.getByText(/Compatible: 5\/5 · Ready: 2/)).toBeInTheDocument();
    expect(screen.getByText('No PETG available (2)')).toBeInTheDocument();
    expect(screen.getByText('Channel 3')).toBeInTheDocument();
  });

  it('keeps relaxed default and emits an explicit feed policy', async () => {
    const onChange = vi.fn();
    render(<AutoModeOptions options={DEFAULT_AUTO_MODE_OPTIONS} onChange={onChange} printers={[]} preview={preview} />);
    expect(screen.getByRole('switch', { name: 'Force exact color match' })).toHaveAttribute('aria-checked', 'false');
    await userEvent.selectOptions(screen.getByLabelText('Filament source'), 'external_only');
    expect(onChange).toHaveBeenCalledWith({ ...DEFAULT_AUTO_MODE_OPTIONS, feed_policy: 'external_only' });
  });

  it('uses the family filament type by default and lets the operator require the exact preset', async () => {
    const onChange = vi.fn();
    render(<AutoModeOptions options={DEFAULT_AUTO_MODE_OPTIONS} onChange={onChange} printers={[]} preview={preview} />);
    await userEvent.click(screen.getByRole('switch', { name: 'Allow base-material match' }));
    expect(onChange).toHaveBeenCalledWith({ ...DEFAULT_AUTO_MODE_OPTIONS, allow_base_material_match: false });
  });

  it('pins a used channel without changing the global color policy', async () => {
    const onChange = vi.fn();
    const onOverridesChange = vi.fn();
    render(<AutoModeOptions options={DEFAULT_AUTO_MODE_OPTIONS} onChange={onChange} printers={[]}
      preview={preview} onOverridesChange={onOverridesChange} />);
    await userEvent.click(screen.getByLabelText('Require this color'));
    expect(onOverridesChange).toHaveBeenCalledWith([{ slot_id: 3, force_color_match: true }]);
    expect(onChange).not.toHaveBeenCalled();
  });
});


it('queues with confirmed compatible candidates despite incomplete farm checks and keeps the selected color rule', async () => {
  const posts: Record<string, unknown>[] = [];
  server.use(
    http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [{ index: 15, name: 'Part' }] })),
    http.post('/api/v1/auto-queue/routing-preview', () => HttpResponse.json({ ...preview, advisory_unavailable: true })),
    http.post('/api/v1/auto-queue/', async ({ request }) => {
      posts.push(await request.json() as Record<string, unknown>);
      return HttpResponse.json({ id: 1, status: 'pending' });
    }),
  );
  render(<PrintModal mode="add-to-queue" archiveId={1} archiveName="Part" initialDispatchMode="auto"
    preselectedPlateId={15} onClose={vi.fn()} />);
  const button = await screen.findByRole('button', { name: /Add to Queue/i });
  await waitFor(() => expect(button).toBeEnabled());
  expect(screen.getByText(/Some printers could not be checked/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('switch', { name: 'Force exact color match' }));
  await waitFor(() => expect(button).toBeEnabled());
  await userEvent.click(button);
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0]).toMatchObject({ force_color_match: true, feed_policy: 'auto', plate_id: 15 });
});


it('waits for source requirements and keeps an explicit false when an older preview arrives late', async () => {
  let releaseInitial!: () => void;
  const pendingInitial = new Promise<void>(resolve => { releaseInitial = resolve; });
  let releaseStrict!: () => void;
  const pendingStrict = new Promise<void>(resolve => { releaseStrict = resolve; });
  let strictStarted = false;
  let previews = 0;
  const posts: Record<string, unknown>[] = [];
  server.use(
    http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [{ index: 15, name: 'Part' }] })),
    http.post('/api/v1/auto-queue/routing-preview', async ({ request }) => {
      const input = await request.json() as { force_color_match: boolean };
      previews += 1;
      if (previews === 1) await pendingInitial;
      if (input.force_color_match) { strictStarted = true; await pendingStrict; }
      return HttpResponse.json(preview);
    }),
    http.post('/api/v1/auto-queue/', async ({ request }) => {
      posts.push(await request.json() as Record<string, unknown>);
      return HttpResponse.json({ id: 1, status: 'pending' });
    }),
  );
  render(<PrintModal mode="add-to-queue" archiveId={1} archiveName="Part" initialDispatchMode="auto"
    preselectedPlateId={15} onClose={vi.fn()} />);
  const button = await screen.findByRole('button', { name: /Add to Queue/i });
  expect(button).toBeDisabled();
  await act(async () => { releaseInitial(); });
  await waitFor(() => expect(button).toBeEnabled());
  await userEvent.click(screen.getByRole('switch', { name: 'Force exact color match' }));
  await waitFor(() => expect(strictStarted).toBe(true));
  await userEvent.click(screen.getByRole('switch', { name: 'Force exact color match' }));
  await act(async () => { releaseStrict(); });
  await waitFor(() => expect(button).toBeEnabled());
  expect(screen.getByRole('switch', { name: 'Force exact color match' })).toHaveAttribute('aria-checked', 'false');
  await userEvent.click(button);
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].force_color_match).toBe(false);
});
