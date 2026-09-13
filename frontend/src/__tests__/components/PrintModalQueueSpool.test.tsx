/**
 * Adding a print now copies its file into BamDude's own data directory first
 * (spec §5/§10, m173), and the dialog has to be honest about that wait and
 * about every way it can end.
 *
 * What is pinned here:
 * - the busy state names what is happening ("saving the file for the queue")
 *   and promises no percentage — there is no progress API to read one from;
 * - success appears only after the server has committed, never on the press;
 * - a second submit while the first is in flight posts NOTHING — one press,
 *   one job. The dialog is driven through the FORM rather than the button,
 *   because that is the hole: the button disables itself on the next render,
 *   and Enter (or a click that beats that render) does not go through it;
 * - every refusal answers the operator's actual question — *was the job
 *   created?* — and then says why. `source_copy_busy` is the normal outcome of
 *   a burst (the quantity mode fires one request per printer while only two
 *   copies run at once), so it must read as "try again in a moment";
 * - a dropped connection is NOT a refusal: the outcome is unknown, so the list
 *   is refreshed and the POST is never repeated — a second POST is a second job.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { delay, http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockShowToast = vi.fn();
vi.mock('../../contexts/ToastContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/ToastContext')>();
  return { ...actual, useToast: () => ({ showToast: mockShowToast }) };
});

// ⚠️ The refresh is the ONE thing the "no answer came back" message tells the
// operator to rely on, so it is asserted as a CALL. Asserting only the sentence
// let the call be deleted with every test still green — a message that lies about
// the single action it promises.
const invalidateQueueViews = vi.fn();
vi.mock('../../utils/queryInvalidation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/queryInvalidation')>();
  return {
    ...actual,
    invalidateQueueViews: (...args: Parameters<typeof actual.invalidateQueueViews>) => {
      invalidateQueueViews(...args);
      return actual.invalidateQueueViews(...args);
    },
  };
});

import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';

const printers = [
  { id: 1, name: 'A1-01', model: 'A1', ip_address: '192.168.1.101', enabled: true, is_active: true },
  { id: 2, name: 'A1-02', model: 'A1', ip_address: '192.168.1.102', enabled: true, is_active: true },
  { id: 3, name: 'A1-03', model: 'A1', ip_address: '192.168.1.103', enabled: true, is_active: true },
];

/** `{code, params, message}` — what `queue_source_capture._refusal` answers with. */
const refusal = (code: string, status: number) =>
  HttpResponse.json({ detail: { code, params: {}, message: `server text for ${code}` } }, { status });

describe('the print dialog while the queue saves the file', () => {
  /** Which printer each POST was for, in order — a count cannot tell a second
   *  job on the printer that already took one from a retry of the two that
   *  refused, which is the whole question in the partial case. */
  let sentTo: number[];
  let posts: number;

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sentTo = [];
    posts = 0;
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [] }),
      ),
      http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
      http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
    );
  });

  const mount = (printerIds: number[] = [1]) =>
    render(
      <PrintModal
        mode="add-to-queue"
        archiveId={1}
        archiveName="Bracket"
        initialSelectedPrinterIds={printerIds}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

  it('says it is saving the file for the queue, and promises no percentage', async () => {
    server.use(
      http.post('/api/v1/queue/', async () => {
        posts += 1;
        await delay(200);
        return HttpResponse.json({ id: 1, status: 'pending', created_item_ids: [1] });
      }),
    );
    mount();

    fireEvent.click(await screen.findByRole('button', { name: /^add to queue$/i }));

    expect(await screen.findByText('Saving the file for the queue…')).toBeInTheDocument();
    // No progress API exists, so nothing on screen may imply one.
    expect(screen.queryByText(/%/)).toBeNull();
    // And nothing claims success while the copy is still being made.
    expect(mockShowToast).not.toHaveBeenCalled();

    await waitFor(() => expect(mockShowToast).toHaveBeenCalledWith('Print queued'));
  });

  it('ignores a second submit while the first is still in flight', async () => {
    server.use(
      http.post('/api/v1/queue/', async () => {
        posts += 1;
        await delay(150);
        return HttpResponse.json({ id: posts, status: 'pending', created_item_ids: [posts] });
      }),
    );
    mount();
    await screen.findByRole('button', { name: /^add to queue$/i });
    // The modal is portalled into `body`, so the form is not under `container`.
    const form = document.querySelector('form')!;

    fireEvent.submit(form);
    fireEvent.submit(form);
    fireEvent.submit(form);

    await waitFor(() => expect(mockShowToast).toHaveBeenCalledWith('Print queued'));
    expect(posts).toBe(1);
  });

  it('reads a busy queue as "try again in a moment", and says nothing was added', async () => {
    server.use(
      http.post('/api/v1/queue/', () => {
        posts += 1;
        return refusal('source_copy_busy', 503);
      }),
    );
    mount();

    fireEvent.click(await screen.findByRole('button', { name: /^add to queue$/i }));

    await waitFor(() =>
      expect(mockShowToast).toHaveBeenCalledWith(
        'Nothing was added to the queue. The queue is already saving other files — try again in a moment.',
        'error',
      ),
    );
    expect(posts).toBe(1);
  });

  it('names an unreadable original and still answers "was it added?" first', async () => {
    server.use(http.post('/api/v1/queue/', () => refusal('source_unreadable', 422)));
    mount();

    fireEvent.click(await screen.findByRole('button', { name: /^add to queue$/i }));

    await waitFor(() =>
      expect(mockShowToast).toHaveBeenCalledWith(
        'Nothing was added to the queue. The original file could not be read. '
          + 'Check that its folder or share is reachable.',
        'error',
      ),
    );
  });

  it('counts what landed, names who refused, and unticks what already has the job', async () => {
    server.use(
      http.post('/api/v1/queue/', async ({ request }) => {
        const body = (await request.json()) as { queue_id: number };
        posts += 1;
        sentTo.push(body.queue_id);
        if (body.queue_id === 1) return HttpResponse.json({ id: 1, status: 'pending', created_item_ids: [1] });
        return refusal('source_copy_busy', 503);
      }),
    );
    mount([1, 2, 3]);

    fireEvent.click(await screen.findByRole('button', { name: /queue to 3 printers/i }));

    await waitFor(() =>
      expect(mockShowToast).toHaveBeenCalledWith(
        'Added to the queue: 1 of 3. The other 2 were not added. '
          + 'A1-02, A1-03: The queue is already saving other files — try again in a moment. '
          + 'The printers that already took it are unticked, so pressing Add again cannot give them a second job.',
        'error',
      ),
    );
  });

  it("never reads one printer's reason as every printer's", async () => {
    // A busy spool and an offline printer are two different answers. Appending
    // only the first as if it explained both hid the second entirely and told
    // the operator to "try again in a moment" about a machine that will not
    // accept the job at all.
    server.use(
      http.post('/api/v1/queue/', async ({ request }) => {
        const body = (await request.json()) as { queue_id: number };
        posts += 1;
        sentTo.push(body.queue_id);
        if (body.queue_id === 1) return HttpResponse.json({ id: 1, status: 'pending', created_item_ids: [1] });
        if (body.queue_id === 2) return refusal('source_copy_busy', 503);
        return HttpResponse.json({ detail: 'Printer offline' }, { status: 409 });
      }),
    );
    mount([1, 2, 3]);

    fireEvent.click(await screen.findByRole('button', { name: /queue to 3 printers/i }));

    await waitFor(() => expect(mockShowToast).toHaveBeenCalled());
    const message = mockShowToast.mock.calls[0][0] as string;
    expect(message).toContain('A1-02: The queue is already saving other files');
    expect(message).toContain('A1-03: Printer offline');
  });

  it('cannot give a second job to the printer that already took one', async () => {
    // The dialog stays open after a partial refusal, and «try again in a moment»
    // is advice the operator will follow. Before the deselect it gave printer 1
    // a SECOND job on the next press; the in-flight ref cannot help, because
    // this is a fresh press after the first submit finished.
    server.use(
      http.post('/api/v1/queue/', async ({ request }) => {
        const body = (await request.json()) as { queue_id: number };
        posts += 1;
        sentTo.push(body.queue_id);
        if (body.queue_id === 1) return HttpResponse.json({ id: 1, status: 'pending', created_item_ids: [1] });
        return refusal('source_copy_busy', 503);
      }),
    );
    mount([1, 2, 3]);

    fireEvent.click(await screen.findByRole('button', { name: /queue to 3 printers/i }));
    await waitFor(() => expect(mockShowToast).toHaveBeenCalled());

    // The button renamed itself: two printers are left ticked.
    fireEvent.click(await screen.findByRole('button', { name: /queue to 2 printers/i }));
    await waitFor(() => expect(posts).toBe(5));

    expect(sentTo).toEqual([1, 2, 3, 2, 3]);
    expect(sentTo.filter((id) => id === 1)).toHaveLength(1);
  });

  it("keeps a refusal's reason when a sibling's answer never came back", async () => {
    // An unanswered request is unknown, not refused — but the printer that DID
    // answer gave the one actionable fact in the whole exchange, and losing it
    // to its sibling's uncertainty is the same defect as misattributing it.
    server.use(
      http.post('/api/v1/queue/', async ({ request }) => {
        const body = (await request.json()) as { queue_id: number };
        posts += 1;
        sentTo.push(body.queue_id);
        if (body.queue_id === 1) return HttpResponse.json({ id: 1, status: 'pending', created_item_ids: [1] });
        if (body.queue_id === 2) return refusal('source_unreadable', 422);
        return HttpResponse.error();
      }),
    );
    mount([1, 2, 3]);

    fireEvent.click(await screen.findByRole('button', { name: /queue to 3 printers/i }));

    await waitFor(() => expect(mockShowToast).toHaveBeenCalled());
    const message = mockShowToast.mock.calls[0][0] as string;
    expect(message).toContain('Added to the queue: 1 of 3.');
    expect(message).toContain('it is not clear whether they were added');
    expect(message).toContain('A1-02: The original file could not be read.');
  });

  it('refreshes the list instead of re-posting when the answer never arrives', async () => {
    server.use(
      http.post('/api/v1/queue/', () => {
        posts += 1;
        return HttpResponse.error();
      }),
    );
    mount();

    fireEvent.click(await screen.findByRole('button', { name: /^add to queue$/i }));

    await waitFor(() =>
      expect(mockShowToast).toHaveBeenCalledWith(
        'It is not clear whether the job was added — no answer came back. '
          + 'The queue has been refreshed; check it before adding the job again.',
        'error',
      ),
    );
    // One attempt, and no retry of its own accord: a second POST is a second job.
    expect(posts).toBe(1);
    // And the list really was refreshed, not just claimed to be.
    expect(invalidateQueueViews).toHaveBeenCalled();
  });
  it('does not claim the ones that landed were lost too', async () => {
    // Two answered, one never did. Neither "nothing was added" nor "the other
    // one was not added" is true of that, and both would send the operator to
    // do the wrong thing.
    server.use(
      http.post('/api/v1/queue/', () => {
        posts += 1;
        if (posts === 1) return HttpResponse.json({ id: 1, status: 'pending', created_item_ids: [1] });
        return HttpResponse.error();
      }),
    );
    mount([1, 2, 3]);

    fireEvent.click(await screen.findByRole('button', { name: /queue to 3 printers/i }));

    await waitFor(() =>
      expect(mockShowToast).toHaveBeenCalledWith(
        'Added to the queue: 1 of 3. No answer came back for the rest, so it is not clear whether they were added. '
          + 'The queue has been refreshed; check it before adding them again.',
        'error',
      ),
    );
    expect(posts).toBe(3);
  });
});
