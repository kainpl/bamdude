import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api, ApiError, type Archive, type PrintStateIssue, type PrintStatePreview } from '../../api/client';
import { PrintStateIssuesPanel } from '../../components/PrintStateIssuesPanel';
import { PrintStateRecoveryDialog } from '../../components/PrintStateRecoveryDialog';
import { RecoveryEndTime } from '../../components/RecoveryEndTime';

const access = vi.hoisted(() => ({ allowed: true }));
vi.mock('../../contexts/AuthContext', async importOriginal => ({
  ...await importOriginal<typeof import('../../contexts/AuthContext')>(),
  useAuth: () => ({ hasAllPermissions: () => access.allowed }),
}));
const issue: PrintStateIssue = {
  printer_id: 7, printer_name: 'P1S farm', queue_id: 4, kind: 'item', target_id: 11, item_id: 11, archive_id: 19,
  code: 'terminal_archive_active_item', actions: ['align_terminal_item'], blocked_reasons: [],
  live_mode: 'inactive', observed_at: 10, started_at: '2026-10-01T10:00:00Z', completed_at: null,
};
const preview: PrintStatePreview = {
  preview_token: 'opaque-preview', action: 'align_terminal_item', archive_preserved: true, expires_in: 300,
  before: { queue: { status: 'printing', is_paused: false }, item: { status: 'printing' }, archive: {} },
  after: { queue: { status: 'paused', is_paused: true }, item: { status: 'completed' }, archive: {}, delete_item: false },
};
function panel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={queryClient}><PrintStateIssuesPanel /></QueryClientProvider>);
}
beforeEach(() => {
  vi.restoreAllMocks(); access.allowed = true;
  vi.spyOn(api, 'getPrintStateIssues').mockResolvedValue({ issues: [issue], next_cursor: null, truncated: false, checked_at: '2026-10-01T12:00:00Z' });
  vi.spyOn(api, 'previewPrintState').mockResolvedValue(preview);
  vi.spyOn(api, 'applyPrintState').mockResolvedValue({ id: 'audit', action: 'align_terminal_item', printer_id: 7, queue_id: 4, item_id: 11, archive_id: 19 });
  vi.spyOn(api, 'getPrintStateHistory').mockResolvedValue({ actions: [], next_cursor: null });
});

describe('Print-state diagnostics', () => {
  it('does not request or expose diagnostics without all read permissions', () => {
    access.allowed = false; panel();
    expect(screen.queryByText('Print state diagnostics')).not.toBeInTheDocument();
    expect(api.getPrintStateIssues).not.toHaveBeenCalled();
  });
  it('omits deletion entirely when the server has not proven it safe', async () => {
    panel(); await screen.findByText(/P1S farm/);
    expect(screen.getByRole('button', { name: 'Align item with archive' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Remove stale item/ })).not.toBeInTheDocument();
  });
  it('renders a request failure separately from an empty healthy page', async () => {
    vi.mocked(api.getPrintStateIssues).mockRejectedValue(new Error('offline'));
    panel(); expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByText(/No inconsistencies/)).not.toBeInTheDocument();
  });
  it('uses explicit cursors even when a partial page contains no issues', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValueOnce({ issues: [], next_cursor: 'next', truncated: true, checked_at: '' });
    const user = userEvent.setup(); panel();
    await user.click(await screen.findByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(api.getPrintStateIssues).toHaveBeenLastCalledWith({ printer_id: undefined, cursor: 'next' }));
  });
  it('loads an addressed recovery history without granting mutation', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValue({ issues: [{ ...issue, actions: [] }], next_cursor: null, truncated: false, checked_at: '' });
    const user = userEvent.setup(); panel();
    await user.click(await screen.findByRole('button', { name: 'Recovery history' }));
    expect(await screen.findByText('No operator recovery actions for this printer.')).toBeInTheDocument();
    expect(api.getPrintStateHistory).toHaveBeenCalledWith(7, undefined);
    expect(api.applyPrintState).not.toHaveBeenCalled();
  });
});

describe('Addressed recovery confirmation', () => {
  it('requires completion confirmation before an estimate and a second confirmation of the delta', async () => {
    const user = userEvent.setup();
    render(<PrintStateRecoveryDialog issue={issue} action="fill_estimated_end_and_align" onClose={vi.fn()} onApplied={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Preview change' })).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: /I confirm that this archive/ }));
    await user.click(screen.getByRole('button', { name: 'Preview change' }));
    expect(await screen.findByText(/queue will stay paused/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply this change' })).toBeDisabled();
    expect(api.applyPrintState).not.toHaveBeenCalled();
    await user.click(screen.getByRole('checkbox', { name: /I reviewed this change/ }));
    await user.click(screen.getByRole('button', { name: 'Apply this change' }));
    expect(api.applyPrintState).toHaveBeenCalledWith(7, 'opaque-preview');
  });
  it('discards a conflicted preview and never reapplies changed data automatically', async () => {
    vi.mocked(api.applyPrintState).mockRejectedValueOnce(new ApiError('changed', 409));
    const user = userEvent.setup();
    render(<PrintStateRecoveryDialog issue={issue} action="align_terminal_item" onClose={vi.fn()} onApplied={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Preview change' }));
    await user.click(await screen.findByRole('checkbox', { name: /I reviewed/ }));
    await user.click(screen.getByRole('button', { name: 'Apply this change' }));
    await screen.findByRole('alert');
    expect(screen.queryByRole('button', { name: 'Apply this change' })).not.toBeInTheDocument();
    expect(api.previewPrintState).toHaveBeenCalledTimes(1);
    expect(api.applyPrintState).toHaveBeenCalledTimes(1);
  });
  it('retries a lost response with the same token so the server can return its audit', async () => {
    vi.mocked(api.applyPrintState).mockRejectedValueOnce(new Error('response lost'));
    const user = userEvent.setup();
    render(<PrintStateRecoveryDialog issue={issue} action="align_terminal_item" onClose={vi.fn()} onApplied={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Preview change' }));
    await user.click(await screen.findByRole('checkbox', { name: /I reviewed/ }));
    await user.click(screen.getByRole('button', { name: 'Apply this change' }));
    await screen.findByRole('alert');
    await user.click(screen.getByRole('button', { name: 'Apply this change' }));
    expect(vi.mocked(api.applyPrintState).mock.calls).toEqual([[7, 'opaque-preview'], [7, 'opaque-preview']]);
    expect(api.previewPrintState).toHaveBeenCalledTimes(1);
  });
  it('labels estimated archive ends, leaving ordinary archive cards unchanged', () => {
    const archive = { completed_at: '2026-10-01T12:00:00Z' } as Archive;
    const { rerender } = render(<RecoveryEndTime archive={archive} />);
    expect(screen.queryByText(/Estimated end/)).not.toBeInTheDocument();
    rerender(<RecoveryEndTime archive={{ ...archive, end_time_source: 'slicer_estimate' }} />);
    expect(screen.getByText(/Estimated end: start/)).toBeInTheDocument();
  });
});

describe('Interrupted dispatch', () => {
  const interrupted: PrintStateIssue = { ...issue, code: 'interrupted_dispatch', actions: ['close_interrupted_attempt'] };
  const closePreview: PrintStatePreview = {
    preview_token: 'close-preview', action: 'close_interrupted_attempt', archive_preserved: true, expires_in: 300,
    before: { queue: { status: 'printing', current_item_id: 11 }, item: { status: 'printing' }, archive: { status: 'printing' } },
    after: {
      queue: { current_item_id: null, status: 'error' }, item: { status: 'failed', error_code: 'dispatchNoAcknowledgement' },
      archive: { status: 'failed', failure_reason: 'dispatchNoAcknowledgement' }, delete_item: false,
    },
  };

  it('shows why an attempt cannot be closed yet', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValue({
      issues: [{ ...interrupted, actions: [], blocked_reasons: ['owner_unknown'] }], next_cursor: null, truncated: false, checked_at: '',
    });
    panel();
    expect(await screen.findByText(/A dispatch was interrupted before the printer was seen printing/)).toBeInTheDocument();
    expect(screen.getByText(/process that owned this dispatch has ended/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Close the interrupted attempt' })).not.toBeInTheDocument();
  });

  it('closes the attempt and leaves Retry and Resume to the operator, promising nothing about the plate', async () => {
    vi.mocked(api.previewPrintState).mockResolvedValue(closePreview);
    const user = userEvent.setup();
    render(<PrintStateRecoveryDialog issue={interrupted} action="close_interrupted_attempt" onClose={vi.fn()} onApplied={vi.fn()} />);
    expect(screen.getByText(/does not prove that nothing was printed/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Preview change' }));
    expect((await screen.findAllByText('Dispatch: no acknowledgement')).length).toBe(2);
    expect(screen.getByText(/use Retry on the job, then resume the queue/)).toBeInTheDocument();
    expect(screen.queryByText(/queue will stay paused/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole('checkbox', { name: /I reviewed this change/ }));
    await user.click(screen.getByRole('button', { name: 'Apply this change' }));
    expect(api.applyPrintState).toHaveBeenCalledWith(7, 'close-preview');
  });
});

describe('A finished run held in memory', () => {
  const held: PrintStateIssue = {
    printer_id: 7, printer_name: 'P1S farm', queue_id: 4, kind: 'runtime', target_id: 19, item_id: null, archive_id: 19,
    code: 'terminal_runtime_context', actions: ['clear_terminal_runtime_context'], blocked_reasons: [],
    live_mode: 'unknown', observed_at: 10, started_at: '2026-10-01T10:00:00Z', completed_at: '2026-10-01T11:00:00Z',
    context: { archive_id: 19, queue_item_id: 11, origin: 'dispatch' }, queue_paused: false, plate_gate: false,
    preheat_pinned: null, temperatures: null, age_seconds: 42,
  };
  const page = (issues: PrintStateIssue[]) => ({ issues, next_cursor: null, truncated: false, checked_at: '2026-10-01T12:00:00Z' });

  it('says the printer is free while archive A is still held, and offers the release', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValue(page([held]));
    panel();
    expect(await screen.findByText(/Archive #19 has finished, but its context is still active/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Release the finished context' })).toBeInTheDocument();
  });

  it('explains a blocked context and offers no button', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValue(page([{
      ...held, actions: [], blocked_reasons: ['context_owner_active', 'swap_debt'],
    }]));
    panel();
    expect(await screen.findByText(/still uses this run/)).toBeInTheDocument();
    expect(screen.getByText(/swap is still owed/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Release the finished context' })).not.toBeInTheDocument();
  });

  it('shows a printer without a queue as diagnostics only', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValue(page([{ ...held, queue_id: null, actions: [], blocked_reasons: ['queue_missing'] }]));
    panel();
    expect(await screen.findByText(/has no queue/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Release the finished context' })).not.toBeInTheDocument();
  });

  it('names a stalled start and points at the context that holds it', async () => {
    const stalled = {
      ...held, kind: 'episode', target_id: 11, item_id: 11, code: 'dispatch_no_progress', actions: [],
      blocked_reasons: ['current_run_conflict'], attempts: 4, span_seconds: 120, related: { kind: 'runtime', target_id: 19 },
    } as unknown as PrintStateIssue;
    vi.mocked(api.getPrintStateIssues).mockResolvedValue(page([held, stalled]));
    panel();
    expect(await screen.findByText(/Start deferred 4 times without progress/)).toBeInTheDocument();
    expect(screen.getByText(/another print is still bound to this printer/i)).toBeInTheDocument();
    const link = screen.getByRole('link', { name: 'Show what holds the printer' });
    expect(link).toHaveAttribute('href', '#print-state-7-runtime-19');
    expect(document.getElementById('print-state-7-runtime-19')).not.toBeNull();
  });

  it('shows a residual preheat with the fresh temperatures or says they are unknown', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValue(page([
      { ...held, preheat_pinned: ['bed', 'chamber'], temperatures: { bed: 58, bed_target: 60, chamber: 35, chamber_target: 40 } },
      { ...held, printer_id: 8, preheat_pinned: ['bed'], temperatures: null },
    ]));
    panel();
    expect(await screen.findByText(/Bed 58\/60 °C/)).toBeInTheDocument();
    expect(screen.getByText(/Current heating is unknown/)).toBeInTheDocument();
    expect(screen.getAllByText(/does not turn the heaters off/).length).toBe(2);
  });

  it('shows the pair of an in-memory repair as one history entry, and an unknown result as unknown', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValue(page([{ ...held, actions: [] }]));
    vi.mocked(api.getPrintStateHistory).mockResolvedValue({ next_cursor: null, actions: [
      { id: 'op-1', action: 'clear_terminal_runtime_context', printer_id: 7, queue_id: 4, item_id: null, archive_id: 19,
        created_at: '2026-10-01T12:00:00Z', actor_label: 'User #1',
        outcome: { id: 'out-1', operation_id: 'op-1', outcome: 'applied', reason: null }, process_ended: false },
      { id: 'op-2', action: 'clear_terminal_runtime_context', printer_id: 7, queue_id: 4, item_id: null, archive_id: 19,
        created_at: '2026-10-01T11:00:00Z', actor_label: 'User #1', outcome: null, process_ended: true },
    ] });
    const user = userEvent.setup(); panel();
    await user.click(await screen.findByRole('button', { name: 'Recovery history' }));
    expect(await screen.findByText(/context released/)).toBeInTheDocument();
    expect(screen.getByText(/process ended before a confirmed result/)).toBeInTheDocument();
    // An in-memory repair names an archive, never an empty queue item.
    expect(screen.queryByText(/Queue item #—/)).not.toBeInTheDocument();
  });

  it('shows a start or completion past its budget as an observation, not a contradiction', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValue(page([{
      ...held, code: 'runtime_phase_lingering', archive_id: null, context: null, actions: [], observation: true,
      blocked_reasons: ['finishing_in_progress'], age_seconds: 450,
    }]));
    panel();
    expect(await screen.findByText(/still in progress/)).toBeInTheDocument();
    expect(screen.getByText(/longer than its usual budget/)).toBeInTheDocument();
    expect(screen.queryByText(/does not match the records/)).not.toBeInTheDocument();
  });

  it('says a stall that went quiet is inactive, not resolved, and counts its attempts', async () => {
    const quiet = {
      ...held, kind: 'episode', target_id: 11, item_id: 11, code: 'dispatch_no_progress', actions: [],
      blocked_reasons: ['current_run_conflict'], attempts: 1, span_seconds: 0, related: null,
      active: false, quiet_seconds: 420, observation: true,
    } as unknown as PrintStateIssue;
    vi.mocked(api.getPrintStateIssues).mockResolvedValue(page([quiet]));
    panel();
    expect(await screen.findByText(/Start deferred 1 time without progress/)).toBeInTheDocument();
    expect(screen.getByText(/No new attempt for 420 s/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Show what holds the printer' })).not.toBeInTheDocument();
  });

  it('says which repair paused the queue, and how it ended', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValue(page([{
      ...held, actions: [], queue_paused: true, blocked_reasons: ['printer_printing'],
      last_repair: { at: '2026-10-01T12:00:00', archive_id: 19, queue_was_paused: false, outcome: 'not_applied_changed',
        reason: 'unavailable', process_ended: false },
    }]));
    panel();
    expect(await screen.findByText(/paused by a release attempt for archive #19/)).toBeInTheDocument();
    expect(screen.getByText(/could not check the printer again/)).toBeInTheDocument();
    expect(screen.getByText(/reports that it is printing/)).toBeInTheDocument();
  });

  it('after a reload, still explains a queue a finished release left paused', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValue(page([{
      ...held, kind: 'repair', target_id: 0, code: 'queue_paused_by_repair', actions: [], observation: true,
      context: undefined, queue_paused: true,
      last_repair: { at: '2026-10-01T12:00:00', archive_id: 19, queue_was_paused: false, outcome: 'applied',
        reason: null, process_ended: false },
    } as PrintStateIssue]));
    panel();
    expect(await screen.findByText(/still paused after a release attempt/)).toBeInTheDocument();
    expect(screen.getByText(/paused by a release attempt for archive #19/)).toBeInTheDocument();
    expect(screen.getByText(/context released/)).toBeInTheDocument();
    expect(screen.getByText(/Check the printer, then resume the queue/)).toBeInTheDocument();
    expect(document.getElementById('print-state-7-repair-0')).not.toBeNull();
    expect(screen.queryByRole('button', { name: 'Release the finished context' })).not.toBeInTheDocument();
  });

  it('names an open layer timelapse as what holds the run', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValue(page([{ ...held, actions: [], blocked_reasons: ['timelapse_open'] }]));
    panel();
    expect(await screen.findByText(/layer timelapse of this print is still open/)).toBeInTheDocument();
  });

  it('says how much of the past the stall records cover', async () => {
    vi.mocked(api.getPrintStateIssues).mockResolvedValue({
      ...page([held]), stall_tracking: { since_seconds: 300, evicted: 2 },
    });
    panel();
    expect(await screen.findByText(/tracked since BamDude started, 5 min ago/)).toBeInTheDocument();
    expect(screen.getByText(/2 stall records were dropped/)).toBeInTheDocument();
  });
});

describe('Releasing a finished context', () => {
  const held: PrintStateIssue = {
    printer_id: 7, printer_name: 'P1S farm', queue_id: 4, kind: 'runtime', target_id: 19, item_id: null, archive_id: 19,
    code: 'terminal_runtime_context', actions: ['clear_terminal_runtime_context'], blocked_reasons: [],
    live_mode: 'unknown', observed_at: 10, started_at: null, completed_at: null,
    context: { archive_id: 19, queue_item_id: 11, origin: 'dispatch' },
  };
  const releasePreview = (queuePaused = false, pinned: string[] | null = null): PrintStatePreview => ({
    preview_token: 'runtime-preview', action: 'clear_terminal_runtime_context', archive_preserved: true, expires_in: 300,
    before: { queue: { is_paused: queuePaused }, item: {}, archive: {} },
    after: {
      queue: { is_paused: true }, item: {}, archive: {}, delete_item: false,
      runtime: { archive_id: 19, admission: true, macro_selection: true, swap_config: false },
    },
    runtime: { context: { archive_id: 19, queue_item_id: 11, origin: 'dispatch' }, queue_paused: queuePaused,
      plate_gate: false, preheat_pinned: pinned, temperatures: null },
  });
  const answer = (outcome: 'applied' | 'not_applied_changed' | 'outcome_unconfirmed', reason: string | null = null) => ({
    id: 'op-1', operation_id: 'op-1', action: 'clear_terminal_runtime_context' as const, printer_id: 7, queue_id: 4,
    item_id: null, archive_id: 19, outcome, reason, queue_paused: true, queue_was_paused: false,
  });

  async function prepareAndApply(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole('button', { name: 'Preview change' }));
    await user.click(await screen.findByRole('checkbox', { name: /I reviewed this change/ }));
    await user.click(screen.getByRole('button', { name: 'Apply this change' }));
  }

  it('says the queue will be paused and is no plate confirmation, and names what goes', async () => {
    vi.mocked(api.previewPrintState).mockResolvedValue(releasePreview());
    const user = userEvent.setup();
    render(<PrintStateRecoveryDialog issue={held} action="clear_terminal_runtime_context" onClose={vi.fn()} onApplied={vi.fn()} />);
    expect(screen.getByText(/The queue will be paused\. Resume it yourself after checking the printer/)).toBeInTheDocument();
    expect(screen.getByText(/Queue item #11 · Archive #19/)).toBeInTheDocument();
    expect(screen.getByText(/does not confirm that the plate is clear/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Preview change' }));
    expect(await screen.findByText(/context of archive #19/)).toBeInTheDocument();
    expect(screen.getByText(/the start it recorded/)).toBeInTheDocument();
    expect(screen.getByText(/the macro selection dispatch registered/)).toBeInTheDocument();
    expect(api.previewPrintState).toHaveBeenCalledWith(7, expect.objectContaining({ kind: 'runtime', target_id: 19 }));
  });

  it('names a pause that was already there and a residual preheat', async () => {
    vi.mocked(api.previewPrintState).mockResolvedValue(releasePreview(true, ['bed']));
    const user = userEvent.setup();
    render(<PrintStateRecoveryDialog issue={held} action="clear_terminal_runtime_context" onClose={vi.fn()} onApplied={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: 'Preview change' }));
    expect(await screen.findByText(/already paused before this repair/)).toBeInTheDocument();
    expect(screen.getByText(/does not turn the heaters off/)).toBeInTheDocument();
  });

  it.each([
    ['applied', null, /context was released/],
    ['not_applied_changed', 'context_changed', /context was not changed/],
    ['outcome_unconfirmed', null, /result was not recorded/],
  ] as const)('reports %s honestly and keeps the dialog open on it', async (outcome, reason, text) => {
    vi.mocked(api.previewPrintState).mockResolvedValue(releasePreview());
    vi.mocked(api.applyPrintState).mockResolvedValue(answer(outcome, reason));
    const onClose = vi.fn(); const onApplied = vi.fn();
    const user = userEvent.setup();
    render(<PrintStateRecoveryDialog issue={held} action="clear_terminal_runtime_context" onClose={onClose} onApplied={onApplied} />);
    await prepareAndApply(user);
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByText(/The queue is paused\. Check the printer/)).toBeInTheDocument();
    expect(onApplied).toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Apply this change' })).not.toBeInTheDocument();
  });

  it('after a refused second step, says the pause stops new starts but cancels none already begun', async () => {
    vi.mocked(api.previewPrintState).mockResolvedValue(releasePreview());
    vi.mocked(api.applyPrintState).mockResolvedValue(answer('not_applied_changed', 'context_changed'));
    const user = userEvent.setup();
    render(<PrintStateRecoveryDialog issue={held} action="clear_terminal_runtime_context" onClose={vi.fn()} onApplied={vi.fn()} />);
    await prepareAndApply(user);
    expect(await screen.findByText(/No new start is scheduled while the queue is paused/)).toBeInTheDocument();
    expect(screen.getByText(/does not cancel a start that had already begun/)).toBeInTheDocument();
  });

  it('says nothing about an attempt already begun once the context was released', async () => {
    vi.mocked(api.previewPrintState).mockResolvedValue(releasePreview());
    vi.mocked(api.applyPrintState).mockResolvedValue(answer('applied'));
    const user = userEvent.setup();
    render(<PrintStateRecoveryDialog issue={held} action="clear_terminal_runtime_context" onClose={vi.fn()} onApplied={vi.fn()} />);
    await prepareAndApply(user);
    expect(await screen.findByText(/context was released/)).toBeInTheDocument();
    expect(screen.queryByText(/already begun/)).not.toBeInTheDocument();
  });
});
