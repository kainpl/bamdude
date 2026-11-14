/**
 * An "Assemble project" job whose dialog was closed is finished by the tracker — and the tracker must not
 * call a job a success when a printer's project is missing: the summary that would have said so is gone.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { api, type AssemblyJobResult, type AssemblyPrinterResult } from '../../api/client';
import { SliceJobTrackerProvider, useSliceJobTracker } from '../../contexts/SliceJobTrackerContext';
import { ToastProvider } from '../../contexts/ToastContext';

function Harness({ onReady }: { onReady: (track: (id: number) => void) => void }) {
  const { trackJob } = useSliceJobTracker();
  onReady((id) => trackJob(id, 'assembly', 'jars'));
  return null;
}

const row = (over: Partial<AssemblyPrinterResult> = {}): AssemblyPrinterResult => ({
  label: 'My X1C',
  model: 'X1C',
  status: 'ok',
  reason: '',
  detail: '',
  parts: [],
  files: [{ library_file_id: 7, name: 'jars — X1C.3mf', plate: null, fallback_reason: null }],
  plates: [],
  fill_counts: {},
  fill_limited: [],
  ...over,
});

const result = (over: Partial<AssemblyJobResult> = {}): AssemblyJobResult => ({
  outcome: 'ok',
  reason: '',
  parts: [],
  printers: [row()],
  trashed: [],
  kept: [],
  ...over,
});

async function finish(state: Record<string, unknown>) {
  vi.useFakeTimers();
  const spy = vi.spyOn(api, 'getSliceJob').mockResolvedValue({ job_id: 1, kind: 'assembly', progress: null, ...state } as never);
  let track: ((id: number) => void) | null = null;
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ToastProvider>
        <SliceJobTrackerProvider>
          <Harness onReady={(fn) => (track = fn)} />
        </SliceJobTrackerProvider>
      </ToastProvider>
    </QueryClientProvider>,
  );
  act(() => track!(1));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1600);
  });
  vi.useRealTimers();
  return spy;
}

describe('SliceJobTracker — an assembly finished after its dialog was closed', () => {
  afterEach(() => vi.restoreAllMocks());

  it('says the project is assembled when every printer got its files', async () => {
    await finish({ status: 'completed', result: result() });
    expect(await screen.findByText('Project assembled: jars')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('asks to be acknowledged when one printer got nothing, naming it and why', async () => {
    const failed = row({ label: 'My A1 mini', model: 'A1MINI', status: 'failed', reason: 'part_rejected', files: [] });
    await finish({ status: 'completed', result: result({ printers: [row(), failed] }) });
    const dialog = await screen.findByRole('dialog', { name: /Assembled only in part/ });
    expect(dialog).toHaveTextContent('A1MINI — My A1 mini: Some parts do not fit this printer');
    expect(screen.queryByText('Project assembled: jars')).not.toBeInTheDocument();
  });

  it('asks to be acknowledged when the previous files were kept', async () => {
    await finish({ status: 'completed', result: result({ kept: [4, 5] }) });
    const dialog = await screen.findByRole('dialog', { name: /Assembled only in part/ });
    expect(dialog).toHaveTextContent('2 previous files were kept.');
  });

  it('shows why a job that saved nothing failed', async () => {
    await finish({ status: 'completed', result: result({ outcome: 'failed', reason: 'source_changed', printers: [] }) });
    const dialog = await screen.findByRole('dialog', { name: /Assembly failed/ });
    expect(dialog).toHaveTextContent('A source file changed while the project was being assembled');
  });

  it('says a stopped job was stopped, without an alarm', async () => {
    await finish({ status: 'failed', error_status: 499, error_detail: 'Canceled' });
    expect(await screen.findByText('Assembly stopped: jars')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
