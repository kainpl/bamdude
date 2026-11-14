import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import {
  DEFAULT_AUTO_MODE_OPTIONS, DEFAULT_PRINT_OPTIONS, DEFAULT_SCHEDULE_OPTIONS,
  DEFAULT_SWAP_MACROS_OPTIONS, type PrintModalAnswer, type PrintModalProps,
} from '../../components/PrintModal/types';

const colorToggle = () => screen.findByRole('checkbox', { name: /force.*color match/i });
const seed: PrintModalAnswer = {
  selectedPrinterIds: [1], dispatchMode: 'specific', autoModeOptions: DEFAULT_AUTO_MODE_OPTIONS,
  scheduleOptions: DEFAULT_SCHEDULE_OPTIONS, quantity: 1, quantityMode: 'perPrinter',
  printOptions: DEFAULT_PRINT_OPTIONS, swapMacros: DEFAULT_SWAP_MACROS_OPTIONS, selectedMacroIds: [],
};

function open(props: Partial<PrintModalProps> = {}, client?: QueryClient) {
  const modal = <PrintModal mode="add-to-queue" archiveId={1} archiveName="Bracket"
    initialSelectedPrinterIds={[1]} onClose={vi.fn()} {...props} />;
  return render(client ? <QueryClientProvider client={client}>{modal}</QueryClientProvider> : modal);
}

describe('the initial colour answer in a real print dialog', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([
        { id: 1, name: 'X1 Carbon', model: 'X1C', ip_address: '192.168.1.100', enabled: true, is_active: true },
      ])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [] })),
      http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
      http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
      http.get('/api/v1/settings/ui-preferences', () => HttpResponse.json({ default_force_color_match: true })),
    );
  });

  it.each([false, true])('initialises a fresh dialog from the saved %s default', async enabled => {
    server.use(http.get('/api/v1/settings/ui-preferences', () => HttpResponse.json({ default_force_color_match: enabled })));
    open();
    const toggle = await colorToggle();
    await waitFor(() => expect(toggle).toHaveProperty('checked', enabled));
    await waitFor(() => expect(screen.getByRole('button', { name: /^add to queue$/i })).toBeEnabled());
  });

  it('does not replace a user answer with a delayed settings response', async () => {
    let finish!: () => void;
    let received!: () => void;
    const requested = new Promise<void>(resolve => { received = resolve; });
    const response = new Promise<void>(resolve => { finish = resolve; });
    server.use(http.get('/api/v1/settings/ui-preferences', async () => {
      received();
      await response;
      return HttpResponse.json({ default_force_color_match: true });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    open({}, client);
    await requested;
    const toggle = await colorToggle();
    expect(screen.getByRole('button', { name: /^add to queue$/i })).toBeDisabled();
    fireEvent.click(toggle);
    fireEvent.click(toggle); // Explicit false, rather than the untouched false placeholder.
    await act(async () => { finish(); });
    await waitFor(() => expect(client.isFetching({ queryKey: ['ui-preferences'] })).toBe(0));
    expect(toggle).not.toBeChecked();
  });

  it('keeps an explicit copied routing answer even when the server default is true', async () => {
    open({ initialRouting: { version: 1, mode: 'auto', feed_policy: 'ams_only', force_color_match: false,
      allow_base_material_match: true, filament_overrides: [] } });
    const toggle = await colorToggle();
    await waitFor(() => expect(screen.getByRole('button', { name: /^add to queue$/i })).toBeEnabled());
    expect(toggle).not.toBeChecked();
  });

  it('keeps a group leader answer and sends it in the queued job', async () => {
    let posted: Record<string, unknown> | undefined;
    server.use(http.post('/api/v1/queue/', async ({ request }) => {
      posted = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ id: 7, created_item_ids: [7] });
    }));
    open({ seededAnswer: seed });
    const toggle = await colorToggle();
    const submit = screen.getByRole('button', { name: /^add to queue$/i });
    await waitFor(() => expect(submit).toBeEnabled());
    expect(toggle).not.toBeChecked();
    fireEvent.click(submit);
    await waitFor(() => expect(posted).toBeDefined());
    expect(posted).toMatchObject({ force_color_match: false });
  });

  it.each(['specific', 'auto'] as const)('sends the initial true default through the %s submit path', async dispatchMode => {
    let posted: Record<string, unknown> | undefined;
    server.use(http.post(dispatchMode === 'auto' ? '/api/v1/auto-queue/' : '/api/v1/queue/', async ({ request }) => {
      posted = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ id: 7, created_item_ids: [7] });
    }));
    open({ initialDispatchMode: dispatchMode });
    const toggle = await screen.findByRole(dispatchMode === 'auto' ? 'switch' : 'checkbox', { name: /force.*color match/i });
    await waitFor(() => expect(toggle).toBeChecked());
    const submit = screen.getByRole('button', { name: /^(add to queue|add to auto.queue)$/i });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(posted).toBeDefined());
    expect(posted).toMatchObject({ force_color_match: true });
  });

  it('reveals a silent dialog when its initial defaults cannot be loaded', async () => {
    server.use(http.get('/api/v1/settings/ui-preferences', () => HttpResponse.json({}, { status: 503 })));
    open({ autoSubmitWhenUnambiguous: true });
    await screen.findByRole('alert');
    expect(screen.getByRole('button', { name: /^add to queue$/i })).toBeDisabled();
  });

  it('does not reset a manually changed choice during a background refetch', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    open({}, client);
    const toggle = await colorToggle();
    await waitFor(() => expect(toggle).toBeChecked());
    fireEvent.click(toggle);
    await act(async () => { await client.invalidateQueries({ queryKey: ['ui-preferences'] }); });
    expect(toggle).not.toBeChecked();
  });

  it.each(['retry', 'current'])('blocks a silent loose fallback on read failure and recovers by %s', async recovery => {
    server.use(http.get('/api/v1/settings/ui-preferences', () => HttpResponse.json({ detail: 'Unavailable' }, { status: 503 })));
    open();
    await screen.findByRole('alert');
    const submit = screen.getByRole('button', { name: /^add to queue$/i });
    expect(submit).toBeDisabled();
    if (recovery === 'retry') {
      server.use(http.get('/api/v1/settings/ui-preferences', () => HttpResponse.json({ default_force_color_match: true })));
      fireEvent.click(screen.getByRole('button', { name: /^retry$/i }));
      await waitFor(async () => expect(await colorToggle()).toBeChecked());
    } else {
      fireEvent.click(screen.getByRole('button', { name: /use current choices/i }));
      expect(await colorToggle()).not.toBeChecked();
    }
    await waitFor(() => expect(submit).toBeEnabled());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
