/**
 * A bug-report recording outlives the panel that started it (upstream #2847).
 *
 * Step 2 asks the user to reproduce the problem, and the panel sits over the
 * part of the app they have to reach to do it. Closing it was the obvious move
 * and it was wrong in two ways, picked by timing alone: reopen inside five
 * minutes and the reset-on-open effect put you on an empty step 1 while the
 * server stayed at DEBUG with nothing left that could stop it; leave it closed
 * and the cap fired behind you and filed the report with no window open.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { BugReportBubble } from '../../components/BugReportBubble';

vi.mock('../../components/ConnectionDiagnostic', () => ({ DiagnosticChecklist: () => null }));
vi.mock('../../components/SystemHealthPanel', () => ({ SystemHealthPanel: () => null }));
vi.mock('../../components/Collapsible', () => ({ Collapsible: () => null }));

const SESSION_KEY = 'bamdude-bug-report-session';

const disc = () => screen.getByTitle(/report a bug|bug report recording/i);
const reproduceStep = () => screen.queryByText('Reproduce the issue now');

beforeEach(() => {
  localStorage.removeItem(SESSION_KEY);
  server.use(
    http.get('*/printers/', () => HttpResponse.json([])),
    http.get('*/system/health', () => HttpResponse.json({ findings: [] })),
  );
});
afterEach(() => localStorage.removeItem(SESSION_KEY));

describe('closing the panel mid-recording', () => {
  it('keeps the run, reopens on step 2 and still submits what was written', async () => {
    const user = userEvent.setup();
    let stopCalls = 0;
    let submitted: { description?: string } | null = null;
    server.use(
      http.post('*/bug-report/start-logging', () => HttpResponse.json({ started: true, was_debug: false })),
      http.post('*/bug-report/stop-logging', () => {
        stopCalls += 1;
        return HttpResponse.json({ logs: 'captured' });
      }),
      http.post('*/bug-report/submit', async ({ request }) => {
        submitted = (await request.json()) as { description?: string };
        return HttpResponse.json({ success: true, message: 'ok', issue_number: 7 });
      }),
    );

    render(<BugReportBubble />);
    await user.click(disc());
    await user.type(screen.getByPlaceholderText(/what went wrong/i), 'Queue page freezes');
    await user.click(screen.getByRole('button', { name: 'Start Debug Logging' }));
    await waitFor(() => expect(reproduceStep()).toBeInTheDocument());

    await user.click(screen.getAllByRole('button').find((b) => b.querySelector('.lucide-x'))!);
    await waitFor(() => expect(reproduceStep()).not.toBeInTheDocument());

    // Closing is not cancelling: only the Stop button brings the log level down.
    expect(stopCalls).toBe(0);
    // The disc says a recording is live, and clicking it gets back to it.
    expect(disc().className).toContain('bg-amber-500');
    expect(localStorage.getItem(SESSION_KEY)).toContain('Queue page freezes');

    await user.click(disc());
    expect(reproduceStep()).toBeInTheDocument();

    // Stopping sends nothing (upstream #3363): the report waits for its own click.
    await user.click(screen.getByRole('button', { name: 'Stop Logging' }));
    await waitFor(() => expect(screen.getByText('Nothing has been sent yet')).toBeInTheDocument());
    expect(stopCalls).toBe(1);
    expect(submitted).toBeNull();
    expect(screen.getByText('1 log line collected')).toBeInTheDocument();

    // Closing and reopening keeps the review and what was written.
    await user.click(screen.getAllByRole('button').find((b) => b.querySelector('.lucide-x'))!);
    await user.click(disc());
    expect(screen.getByText('Nothing has been sent yet')).toBeInTheDocument();
    expect(screen.getByText('Queue page freezes')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Send report' }));
    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted!.description).toBe('Queue page freezes');
    expect(stopCalls).toBe(1);
    expect(localStorage.getItem(SESSION_KEY)).toBeNull();
  });
});

describe('a stopped recording', () => {
  const record = async (user: ReturnType<typeof userEvent.setup>) => {
    render(<BugReportBubble />);
    await user.click(disc());
    await user.type(screen.getByPlaceholderText(/what went wrong/i), 'Card flickers');
    await user.click(screen.getByRole('button', { name: 'Start Debug Logging' }));
    await waitFor(() => expect(reproduceStep()).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: 'Stop Logging' }));
    await waitFor(() => expect(screen.getByText('Nothing has been sent yet')).toBeInTheDocument());
  };

  it('is discarded without sending anything', async () => {
    const user = userEvent.setup();
    let submits = 0;
    server.use(
      http.post('*/bug-report/start-logging', () => HttpResponse.json({ started: true, was_debug: false })),
      http.post('*/bug-report/stop-logging', () => HttpResponse.json({ logs: 'a\nb' })),
      http.post('*/bug-report/submit', () => {
        submits += 1;
        return HttpResponse.json({ success: true, message: 'ok' });
      }),
    );

    await record(user);
    await user.click(screen.getByRole('button', { name: 'Discard' }));

    expect(screen.getByRole('button', { name: 'Start Debug Logging' })).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/what went wrong/i)).toHaveValue('');
    expect(submits).toBe(0);
  });

  it('keeps its logs when the send fails, and sends them on the second try', async () => {
    const user = userEvent.setup();
    const sent: Array<{ debug_logs?: string }> = [];
    server.use(
      http.post('*/bug-report/start-logging', () => HttpResponse.json({ started: true, was_debug: false })),
      http.post('*/bug-report/stop-logging', () => HttpResponse.json({ logs: 'a\nb' })),
      http.post('*/bug-report/submit', async ({ request }) => {
        sent.push((await request.json()) as { debug_logs?: string });
        return sent.length === 1
          ? HttpResponse.json({ success: false, message: 'relay is down' })
          : HttpResponse.json({ success: true, message: 'ok', issue_number: 9 });
      }),
    );

    await record(user);
    await user.click(screen.getByRole('button', { name: 'Send report' }));
    await waitFor(() => expect(screen.getByText('relay is down')).toBeInTheDocument());

    // Reopening the panel on the failure does not reset it to an empty form.
    await user.click(screen.getAllByRole('button').find((b) => b.querySelector('.lucide-x'))!);
    await user.click(disc());
    await user.click(screen.getByRole('button', { name: 'Back to the report' }));
    expect(screen.getByText('2 log lines collected')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Send report' }));
    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[1].debug_logs).toBe('a\nb');
  });
});

describe('after a reload', () => {
  const storeSession = (startedAt: number, description = 'Printer card goes blank') =>
    localStorage.setItem(SESSION_KEY, JSON.stringify({ description, email: '', wasDebug: false, startedAt }));

  it('picks the run back up while the server is still logging', async () => {
    const user = userEvent.setup();
    const startedAt = Date.now() - 30_000;
    storeSession(startedAt);
    server.use(
      http.get('*/support/debug-logging', () =>
        HttpResponse.json({ enabled: true, enabled_at: new Date(startedAt).toISOString(), duration_seconds: 30 }),
      ),
    );

    render(<BugReportBubble />);

    await waitFor(() => expect(disc().className).toContain('bg-amber-500'));
    await user.click(disc());
    expect(reproduceStep()).toBeInTheDocument();
    // Elapsed comes off the run's start time, so the reload does not reset it.
    expect(screen.getByText('00:30')).toBeInTheDocument();
  });

  it('drops a stored run the server already stopped', async () => {
    storeSession(Date.now() - 30_000, 'stale');
    let stopCalls = 0;
    server.use(
      http.get('*/support/debug-logging', () =>
        HttpResponse.json({ enabled: false, enabled_at: null, duration_seconds: null }),
      ),
      http.post('*/bug-report/stop-logging', () => {
        stopCalls += 1;
        return HttpResponse.json({ logs: '' });
      }),
    );

    render(<BugReportBubble />);

    await waitFor(() => expect(localStorage.getItem(SESSION_KEY)).toBeNull());
    expect(disc().className).toContain('bg-red-500');
    expect(stopCalls).toBe(0);
  });

  it('puts the log level back for a run that outlived the cap, without filing it', async () => {
    let stopCalls = 0;
    let submitCalls = 0;
    storeSession(Date.now() - 3_600_000, 'from an hour ago');
    server.use(
      http.get('*/support/debug-logging', () =>
        HttpResponse.json({
          enabled: true,
          enabled_at: new Date(Date.now() - 3_600_000).toISOString(),
          duration_seconds: 3600,
        }),
      ),
      http.post('*/bug-report/stop-logging', () => {
        stopCalls += 1;
        return HttpResponse.json({ logs: '' });
      }),
      http.post('*/bug-report/submit', () => {
        submitCalls += 1;
        return HttpResponse.json({ success: true, message: 'ok' });
      }),
    );

    render(<BugReportBubble />);

    await waitFor(() => expect(stopCalls).toBe(1));
    expect(submitCalls).toBe(0);
    expect(localStorage.getItem(SESSION_KEY)).toBeNull();
    expect(disc().className).toContain('bg-red-500');
  });
});
