/**
 * Filing a print under an order from the dialog that starts it.
 *
 * Before this the only way a print counted against an order was to start it
 * from that order's plan block. Everything else — the File Manager, a printer
 * card, the queue page — produced a print the order could not see, and the
 * operator's own filing happened later, by hand, on the archive. The field
 * asks the question where the print is actually started, and answers it with
 * the order that still needs this plate.
 *
 * ⚠️ The dialog asks ONLY when nobody has already answered. A plan block
 * passes its line, and an archive reprint carries the original print's own
 * binding — both would be a second, weaker source of truth for the same
 * question, so neither gets the field.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { delay, http, HttpResponse } from 'msw';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { server } from '../mocks/server';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import { api, type OrderCandidate } from '../../api/client';

/**
 * Every path that queues from this dialog must mark the proposal stale.
 *
 * The hook caches `outstanding_prints` for 30 s, so a second print of the same
 * file inside that window is offered the count from BEFORE the first one — the
 * dialog says "still needs 5" about work it has already sent. There are three
 * places that can end a submit, and a spy is the only way to hold all three:
 * two of them are success paths and the third is the PARTIAL failure, which is
 * exactly the one somebody adding a fourth would forget.
 *
 * The real helper still runs — this records the call rather than replacing it.
 */
const invalidatedCandidates = vi.hoisted(() => vi.fn());
vi.mock('../../utils/queryInvalidation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/queryInvalidation')>();
  return {
    ...actual,
    invalidateOrderCandidates: (qc: Parameters<typeof actual.invalidateOrderCandidates>[0]) => {
      invalidatedCandidates(qc);
      return actual.invalidateOrderCandidates(qc);
    },
  };
});

/** What the mocked `useAuth` grants. Reset in `beforeEach`, narrowed in the one
 *  test that asks what a caller without `projects:read` sees. */
const auth = vi.hoisted(() => ({ granted: new Set<string>() }));

vi.mock('../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({ ...actual.useAuth(), hasPermission: (p: string) => auth.granted.has(p) }),
  };
});

const mockPrinters = [
  { id: 1, name: 'X1 Carbon', model: 'X1C', ip_address: '192.168.1.100', enabled: true, is_active: true },
  { id: 2, name: 'P1S', model: 'P1S', ip_address: '192.168.1.101', enabled: true, is_active: true },
];

const candidate = (over: Partial<OrderCandidate> = {}): OrderCandidate => ({
  project_id: 4,
  project_name: 'Kickstarter batch',
  project_line_id: 9,
  product_id: 2,
  product_name: 'Desk Lamp',
  outstanding_prints: 5,
  priority: 2,
  deadline: null,
  created_at: '2026-09-01T10:14:02',
  line_material: null,
  ...over,
});

const spareStock = (over: Partial<OrderCandidate> = {}): OrderCandidate =>
  candidate({ project_id: 6, project_name: 'Spare stock', project_line_id: 12, ...over });

const MULTI_PLATE = {
  is_multi_plate: true,
  plates: [
    { index: 1, name: 'Plate 1', has_thumbnail: false, thumbnail_url: null, objects: ['A'], filaments: [], print_time_seconds: 60, filament_used_grams: 1 },
    { index: 2, name: 'Plate 2', has_thumbnail: false, thumbnail_url: null, objects: ['B'], filaments: [], print_time_seconds: 60, filament_used_grams: 1 },
  ],
};

/** Serve a candidate list per plate, recording which plates were asked about. */
function serveCandidates(byPlate: Record<number, OrderCandidate[]>, seen?: number[]) {
  server.use(
    http.get('/api/v1/library/files/:id/order-candidates', ({ request }) => {
      const plate = Number(new URL(request.url).searchParams.get('plate_index') ?? 0);
      seen?.push(plate);
      return HttpResponse.json(byPlate[plate] ?? []);
    }),
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  auth.granted = new Set(['projects:read', 'printers:control']);
  invalidatedCandidates.mockClear();
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json(mockPrinters)),
    http.get('/api/v1/printers/:id/status', () =>
      HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [] }),
    ),
    http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
    http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
    http.get('/api/v1/library/files/:id', () =>
      HttpResponse.json({ id: 5, filename: 'lamp.gcode.3mf', file_tags: ['gcode', '3mf', 'sliced'] }),
    ),
    http.get('/api/v1/library/files/:id/plates', () =>
      HttpResponse.json({ is_multi_plate: false, plates: [] }),
    ),
    http.get('/api/v1/library/files/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
  );
});

describe('PrintModal — the order this print is filed under', () => {
  it('proposes the order that still needs the plate and files the print under it', async () => {
    serveCandidates({ 0: [candidate(), spareStock({ outstanding_prints: 0 })] });
    const add = vi.spyOn(api, 'addToQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    render(<PrintModal mode="add-to-queue" libraryFileId={5} archiveName="lamp.gcode.3mf" onClose={() => {}} />);

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    await waitFor(() => expect(field.value).toBe('4:9'));

    await user.click(screen.getByText('X1 Carbon'));
    await user.click(screen.getByRole('button', { name: /^add to queue$/i }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(expect.objectContaining({ project_id: 4, project_line_id: 9 })),
    );
  });

  it('defaults to the first candidate that still NEEDS prints, not merely the first', async () => {
    // The wire already sorts needy first, but the rule the dialog applies is
    // its own: an order that is already covered is never proposed by default.
    serveCandidates({ 0: [spareStock({ outstanding_prints: 0 }), candidate()] });

    render(<PrintModal mode="add-to-queue" libraryFileId={5} archiveName="lamp.gcode.3mf" onClose={() => {}} />);

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    await waitFor(() => expect(field.value).toBe('4:9'));
  });

  it('defaults to no order when every candidate is already covered', async () => {
    serveCandidates({ 0: [candidate({ outstanding_prints: 0 })] });
    const add = vi.spyOn(api, 'addToQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    render(<PrintModal mode="add-to-queue" libraryFileId={5} archiveName="lamp.gcode.3mf" onClose={() => {}} />);

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    expect(field.value).toBe('');
    // Covered is not the same as gone: printing ahead stays one click away.
    expect(screen.getByRole('option', { name: /already covered/ })).toBeInTheDocument();

    await user.click(screen.getByText('X1 Carbon'));
    await user.click(screen.getByRole('button', { name: /^add to queue$/i }));

    await waitFor(() => expect(add).toHaveBeenCalled());
    expect(add.mock.calls[0][0]).toMatchObject({ project_id: undefined, project_line_id: null });
  });

  it('does not ask when the plan block already named the line', async () => {
    const seen: number[] = [];
    serveCandidates({ 0: [candidate()] }, seen);
    const add = vi.spyOn(api, 'addToQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="lamp.gcode.3mf"
        projectId={3}
        projectLineId={10}
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());
    expect(screen.queryByLabelText('Order')).not.toBeInTheDocument();

    await user.click(screen.getByText('X1 Carbon'));
    await user.click(screen.getByRole('button', { name: /^add to queue$/i }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(expect.objectContaining({ project_id: 3, project_line_id: 10 })),
    );
    expect(seen).toEqual([]);
  });

  it('does not ask when the caller named an order but no line', async () => {
    // "Bound to the order, to no line of it" is a decision somebody already
    // made; re-asking here would let the dialog move the print to a DIFFERENT
    // order, which the caller never offered.
    serveCandidates({ 0: [candidate()] });

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="lamp.gcode.3mf"
        projectId={3}
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());
    expect(screen.queryByLabelText('Order')).not.toBeInTheDocument();
  });

  it('does not ask on a reprint from an archive', async () => {
    serveCandidates({ 0: [candidate()] });

    render(<PrintModal mode="reprint" archiveId={1} archiveName="Benchy" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());
    expect(screen.queryByLabelText('Order')).not.toBeInTheDocument();
  });

  it('re-asks for the plate the operator switched to and re-applies the default', async () => {
    const seen: number[] = [];
    serveCandidates({ 1: [candidate()], 2: [spareStock({ outstanding_prints: 3 })] }, seen);
    server.use(http.get('/api/v1/library/files/:id/plates', () => HttpResponse.json(MULTI_PLATE)));
    const user = userEvent.setup();

    render(<PrintModal mode="reprint" libraryFileId={5} archiveName="lamp.gcode.3mf" onClose={() => {}} />);

    await waitFor(() =>
      expect((screen.getByLabelText('Order') as HTMLSelectElement).value).toBe('4:9'),
    );

    // The numbered button only makes a plate ACTIVE; the card beside it is what
    // selects it — which is the plate the dialog is then about.
    await user.click(screen.getByTitle('Plate 2'));
    await user.click(screen.getByText('Plate 2'));

    // ⚠️ The field is re-queried: while the new plate's candidates are in
    // flight there is nothing to show, so the select is unmounted and mounted
    // again. Holding the old element would read the OLD plate's answer — and
    // showing the old LIST there would be worse still, because its default is a
    // line this plate may not serve at all.
    await waitFor(() =>
      expect((screen.getByLabelText('Order') as HTMLSelectElement).value).toBe('6:12'),
    );
    expect(seen).toContain(2);
  });

  it('never overwrites a choice the operator made, whatever the plate does', async () => {
    serveCandidates({ 1: [candidate()], 2: [spareStock({ outstanding_prints: 3 })] });
    server.use(http.get('/api/v1/library/files/:id/plates', () => HttpResponse.json(MULTI_PLATE)));
    const user = userEvent.setup();

    render(<PrintModal mode="reprint" libraryFileId={5} archiveName="lamp.gcode.3mf" onClose={() => {}} />);

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    await waitFor(() => expect(field.value).toBe('4:9'));

    await user.selectOptions(field, '');
    expect(field.value).toBe('');

    await user.click(screen.getByTitle('Plate 2'));
    await user.click(screen.getByText('Plate 2'));

    await waitFor(() => expect(screen.getByRole('option', { name: /Spare stock/ })).toBeInTheDocument());
    expect((screen.getByLabelText('Order') as HTMLSelectElement).value).toBe('');
  });

  it('drops a chosen order the new plate does not offer, and takes the new default', async () => {
    // ⚠️ The stale half of the same rule that keeps a deliberate answer. A
    // choice the current plate's candidates no longer contain is not an
    // answer about THIS plate — the native select shows «Without an order»
    // for it while the payload would still carry the old order and line, and
    // nothing on screen says so.
    serveCandidates({
      1: [spareStock({ outstanding_prints: 2 }), candidate()],
      2: [spareStock({ outstanding_prints: 2 })],
    });
    server.use(http.get('/api/v1/library/files/:id/plates', () => HttpResponse.json(MULTI_PLATE)));
    const print = vi.spyOn(api, 'printLibraryFile').mockResolvedValue({ status: 'dispatched' } as never);
    const user = userEvent.setup();

    render(<PrintModal mode="reprint" libraryFileId={5} archiveName="lamp.gcode.3mf" onClose={() => {}} />);

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    await waitFor(() => expect(field.value).toBe('6:12'));
    await user.selectOptions(field, '4:9');
    expect(field.value).toBe('4:9');

    await user.click(screen.getByTitle('Plate 2'));
    await user.click(screen.getByText('Plate 2'));

    await waitFor(() =>
      expect((screen.getByLabelText('Order') as HTMLSelectElement).value).toBe('6:12'),
    );

    await user.click(screen.getByText('X1 Carbon'));
    await user.click(screen.getByRole('button', { name: /^print$/i }));

    await waitFor(() =>
      expect(print).toHaveBeenCalledWith(5, 1, expect.objectContaining({ project_id: 6, project_line_id: 12 })),
    );
  });

  it('files nothing when the plate switched to wants no order at all', async () => {
    // The same staleness with the field GONE: no list, nothing to show the
    // operator, and the old plate's ids would ship unseen.
    serveCandidates({ 1: [candidate()], 2: [] });
    server.use(http.get('/api/v1/library/files/:id/plates', () => HttpResponse.json(MULTI_PLATE)));
    const print = vi.spyOn(api, 'printLibraryFile').mockResolvedValue({ status: 'dispatched' } as never);
    const user = userEvent.setup();

    render(<PrintModal mode="reprint" libraryFileId={5} archiveName="lamp.gcode.3mf" onClose={() => {}} />);

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    await waitFor(() => expect(field.value).toBe('4:9'));
    // Touch it, so the choice is the operator's and not merely a proposal.
    await user.selectOptions(field, '');
    await user.selectOptions(screen.getByLabelText('Order'), '4:9');

    await user.click(screen.getByTitle('Plate 2'));
    await user.click(screen.getByText('Plate 2'));

    await waitFor(() => expect(screen.queryByLabelText('Order')).not.toBeInTheDocument());

    await user.click(screen.getByText('X1 Carbon'));
    await user.click(screen.getByRole('button', { name: /^print$/i }));

    await waitFor(() => expect(print).toHaveBeenCalled());
    expect(print.mock.calls[0][2]).toMatchObject({ project_id: undefined, project_line_id: null });
  });

  it('asks about the first ticked plate and files only the ORDER when several are ticked', async () => {
    // ⚠️ The candidates were asked about ONE plate; another plate of the same
    // file can belong to another line, or another product entirely. So the
    // order travels and the line does not — the backend writers resolve the
    // line per row, which is the only place that knows which plate each row is
    // for. `plate_index=0` would have been wrong in the other direction: a
    // product registered per plate index has no whole-file plate, the list
    // comes back empty, the field hides, and both rows file under nothing.
    const seen: number[] = [];
    serveCandidates({ 1: [candidate()], 2: [spareStock({ outstanding_prints: 2 })] }, seen);
    server.use(http.get('/api/v1/library/files/:id/plates', () => HttpResponse.json(MULTI_PLATE)));
    const add = vi.spyOn(api, 'addToAutoQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="lamp.gcode.3mf"
        initialDispatchMode="auto"
        lockDispatchMode
        onClose={() => {}}
      />,
    );

    await user.click(await screen.findByText('Select All 2 Plates'));

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    await waitFor(() => expect(field.value).toBe('4:9'));
    expect(seen).not.toContain(0);

    await user.click(screen.getByRole('button', { name: 'Queue 2 Plates' }));

    await waitFor(() => expect(add).toHaveBeenCalled());
    expect(add.mock.calls[0][0]).toMatchObject({ project_id: 4, project_line_id: null });
  });

  it('files the line as well when exactly one plate is ticked', async () => {
    serveCandidates({ 1: [candidate()], 2: [spareStock({ outstanding_prints: 2 })] });
    server.use(http.get('/api/v1/library/files/:id/plates', () => HttpResponse.json(MULTI_PLATE)));
    const add = vi.spyOn(api, 'addToAutoQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="lamp.gcode.3mf"
        initialDispatchMode="auto"
        lockDispatchMode
        onClose={() => {}}
      />,
    );

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    await waitFor(() => expect(field.value).toBe('4:9'));

    await user.click(screen.getByRole('button', { name: /^add to queue$/i }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(expect.objectContaining({ project_id: 4, project_line_id: 9 })),
    );
  });

  it('will not let the operator submit before the order question has answered', async () => {
    // The manual half of the guard the silent members already had. The window
    // is bounded by the query itself, which does not retry — a failed or
    // disabled query never blocks the button.
    server.use(
      http.get('/api/v1/library/files/:id/order-candidates', async () => {
        await delay(60);
        return HttpResponse.json([candidate()]);
      }),
    );

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="lamp.gcode.3mf"
        initialDispatchMode="auto"
        lockDispatchMode
        onClose={() => {}}
      />,
    );

    const submit = await screen.findByRole('button', { name: /^add to queue$/i });
    expect(submit).toBeDisabled();
    expect(submit).toHaveAttribute('title', 'Checking which order needs this…');

    await waitFor(() => expect(submit).toBeEnabled());
    expect(submit).not.toHaveAttribute('title');
  });

  it('holds a silent member back while the plate list it waits on is still loading', async () => {
    // ⚠️ The guard used to be `asksAboutOrder && orderCandidatesLoading`, and
    // `isLoading` is FALSE for a query that is not enabled yet. The candidates
    // query waits for the plates list to settle, so for the whole of that wait
    // the dialog reported "nothing pending" — and a silent member, which submits
    // the moment nothing is pending, sent its payload with no order on it. It
    // passed every interactive test there is, because a person cannot click
    // faster than a plates fetch.
    let releasePlates: (() => void) | null = null;
    server.use(
      http.get('/api/v1/library/files/:id/plates', async () => {
        await new Promise<void>((resolve) => {
          releasePlates = resolve;
        });
        return HttpResponse.json({ is_multi_plate: false, plates: [] });
      }),
    );
    serveCandidates({ 0: [candidate()] });
    const add = vi.spyOn(api, 'addToAutoQueue').mockResolvedValue({ id: 1 } as never);

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="lamp.gcode.3mf"
        initialDispatchMode="auto"
        lockDispatchMode
        autoSubmitWhenUnambiguous
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(releasePlates).not.toBeNull());
    // Long enough for the member to have done the wrong thing: nothing else it
    // waits on is slow, so with the hole open the submit lands here.
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(add).not.toHaveBeenCalled();

    releasePlates!();

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(expect.objectContaining({ project_id: 4, project_line_id: 9 })),
    );
  });

  it('keeps the submit button disabled while the plate list the question waits on loads', async () => {
    // The visible half of the same window — the button and the silent member
    // are gated on one flag, so a hole in it opens both at once.
    let releasePlates: (() => void) | null = null;
    server.use(
      http.get('/api/v1/library/files/:id/plates', async () => {
        await new Promise<void>((resolve) => {
          releasePlates = resolve;
        });
        return HttpResponse.json({ is_multi_plate: false, plates: [] });
      }),
    );
    serveCandidates({ 0: [candidate()] });

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="lamp.gcode.3mf"
        initialDispatchMode="auto"
        lockDispatchMode
        onClose={() => {}}
      />,
    );

    const submit = await screen.findByRole('button', { name: /^add to queue$/i });
    await waitFor(() => expect(releasePlates).not.toBeNull());
    expect(submit).toBeDisabled();
    expect(submit).toHaveAttribute('title', 'Checking which order needs this…');

    releasePlates!();

    await waitFor(() => expect(submit).toBeEnabled());
  });

  it('does not ask at all without permission to read orders', async () => {
    // ⚠️ The candidates endpoint needs `projects:read` beside the library read —
    // it names orders and how much of each is left. Asking anyway is a
    // guaranteed 403 whose only visible effect is a submit button disabled while
    // it happens and a field that never appears.
    auth.granted = new Set(['printers:control']);
    const seen: number[] = [];
    serveCandidates({ 0: [candidate()] }, seen);
    const add = vi.spyOn(api, 'addToAutoQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="lamp.gcode.3mf"
        initialDispatchMode="auto"
        lockDispatchMode
        onClose={() => {}}
      />,
    );

    const submit = await screen.findByRole('button', { name: /^add to queue$/i });
    await waitFor(() => expect(submit).toBeEnabled());
    expect(screen.queryByLabelText('Order')).not.toBeInTheDocument();
    expect(seen).toEqual([]);

    await user.click(submit);

    await waitFor(() => expect(add).toHaveBeenCalled());
    expect(add.mock.calls[0][0]).toMatchObject({ project_id: undefined, project_line_id: null });
  });

  it('pins the plate list against retrying, whatever the client default is', async () => {
    // The Order question WAITS on this query settling, so three silent backoffs
    // before a permanent failure is admitted is a field that never appears and
    // a submit button disabled for the length of them.
    const client = new QueryClient({ defaultOptions: { queries: { retry: 3, gcTime: 0 } } });
    serveCandidates({ 0: [] });

    render(
      <QueryClientProvider client={client}>
        <PrintModal mode="add-to-queue" libraryFileId={5} archiveName="lamp.gcode.3mf" onClose={() => {}} />
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect(client.getQueryCache().find({ queryKey: ['library-file-plates', 5] })).toBeDefined(),
    );
    expect(client.getQueryCache().find({ queryKey: ['library-file-plates', 5] })?.options.retry).toBe(false);
  });

  it('carries both ids into a direct print of a library file', async () => {
    serveCandidates({ 0: [candidate()] });
    const print = vi.spyOn(api, 'printLibraryFile').mockResolvedValue({ status: 'dispatched' } as never);
    const user = userEvent.setup();

    render(<PrintModal mode="reprint" libraryFileId={5} archiveName="lamp.gcode.3mf" onClose={() => {}} />);

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    await waitFor(() => expect(field.value).toBe('4:9'));

    await user.click(screen.getByText('X1 Carbon'));
    await user.click(screen.getByRole('button', { name: /^print$/i }));

    await waitFor(() =>
      expect(print).toHaveBeenCalledWith(5, 1, expect.objectContaining({ project_id: 4, project_line_id: 9 })),
    );
  });

  it('files a silent member of a grouped run under its own order too', async () => {
    // ⚠️ A grouped run shows ONE dialog and submits the rest without rendering
    // them. The visible one's answer must not travel — a group is several
    // FILES, and another file's plate belongs to another product and so to
    // another order. Each member therefore asks for itself, which means the
    // silent ones have to wait for that answer before submitting; a member that
    // beat its own query would file nothing while the leader beside it filed
    // correctly, and nothing on screen would say so.
    serveCandidates({ 0: [candidate()] });
    const add = vi.spyOn(api, 'addToAutoQueue').mockResolvedValue({ id: 1 } as never);

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="lamp.gcode.3mf"
        initialDispatchMode="auto"
        lockDispatchMode
        autoSubmitWhenUnambiguous
        onClose={() => {}}
      />,
    );

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(expect.objectContaining({ project_id: 4, project_line_id: 9 })),
    );
  });

  it('carries both ids into the auto-queue payload', async () => {
    serveCandidates({ 0: [candidate()] });
    const add = vi.spyOn(api, 'addToAutoQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="lamp.gcode.3mf"
        initialDispatchMode="auto"
        lockDispatchMode
        onClose={() => {}}
      />,
    );

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    await waitFor(() => expect(field.value).toBe('4:9'));

    await user.click(screen.getByRole('button', { name: /^add to queue$/i }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(expect.objectContaining({ project_id: 4, project_line_id: 9 })),
    );
  });

  it('still asks — about the whole file — when the plate list itself fails', async () => {
    // ⚠️ The question waits for the plates query to SETTLE, not to produce data.
    // A plates endpoint that fails permanently never produces any, and waiting
    // on data alone left the Order field missing for ever on such a file —
    // silently, because nothing else in the dialog depends on that list. A
    // settled failure is an answer: ask about plate 0, the whole file.
    const seen: number[] = [];
    serveCandidates({ 0: [candidate()] }, seen);
    server.use(
      http.get('/api/v1/library/files/:id/plates', () => new HttpResponse(null, { status: 500 })),
    );
    const add = vi.spyOn(api, 'addToQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    render(<PrintModal mode="add-to-queue" libraryFileId={5} archiveName="lamp.gcode.3mf" onClose={() => {}} />);

    const field = (await screen.findByLabelText('Order')) as HTMLSelectElement;
    await waitFor(() => expect(field.value).toBe('4:9'));
    expect(seen).toEqual([0]);

    await user.click(screen.getByText('X1 Carbon'));
    await user.click(screen.getByRole('button', { name: /^add to queue$/i }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(expect.objectContaining({ project_id: 4, project_line_id: 9 })),
    );
  });

  describe('the "still needs N" counts are marked stale by every path that queues', () => {
    it('after a printer-queue submit', async () => {
      serveCandidates({ 0: [candidate()] });
      vi.spyOn(api, 'addToQueue').mockResolvedValue({ id: 1 } as never);
      const user = userEvent.setup();

      render(<PrintModal mode="add-to-queue" libraryFileId={5} archiveName="lamp.gcode.3mf" onClose={() => {}} />);
      await waitFor(() => expect((screen.getByLabelText('Order') as HTMLSelectElement).value).toBe('4:9'));

      await user.click(screen.getByText('X1 Carbon'));
      await user.click(screen.getByRole('button', { name: /^add to queue$/i }));

      await waitFor(() => expect(invalidatedCandidates).toHaveBeenCalled());
    });

    it('after an auto-queue submit', async () => {
      serveCandidates({ 0: [candidate()] });
      vi.spyOn(api, 'addToAutoQueue').mockResolvedValue({ id: 1 } as never);
      const user = userEvent.setup();

      render(
        <PrintModal
          mode="add-to-queue"
          libraryFileId={5}
          archiveName="lamp.gcode.3mf"
          initialDispatchMode="auto"
          lockDispatchMode
          onClose={() => {}}
        />,
      );
      await waitFor(() => expect((screen.getByLabelText('Order') as HTMLSelectElement).value).toBe('4:9'));

      await user.click(screen.getByRole('button', { name: /^add to queue$/i }));

      await waitFor(() => expect(invalidatedCandidates).toHaveBeenCalled());
    });

    it('and after a PARTIAL failure, where some of the work did land', async () => {
      // ⚠️ The path that is easy to miss: two printers, one write succeeded.
      // The dialog stays open on this branch, so a stale proposal is not merely
      // cached — it is on screen, above a count that has already moved.
      serveCandidates({ 0: [candidate()] });
      const add = vi
        .spyOn(api, 'addToQueue')
        .mockResolvedValueOnce({ id: 1 } as never)
        .mockRejectedValueOnce(new Error('printer busy'));
      const user = userEvent.setup();

      render(<PrintModal mode="add-to-queue" libraryFileId={5} archiveName="lamp.gcode.3mf" onClose={() => {}} />);
      await waitFor(() => expect((screen.getByLabelText('Order') as HTMLSelectElement).value).toBe('4:9'));

      await user.click(screen.getByText('X1 Carbon'));
      await user.click(screen.getByText('P1S'));
      await user.click(screen.getByRole('button', { name: /queue to 2 printers/i }));

      await waitFor(() => expect(add).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(invalidatedCandidates).toHaveBeenCalled());
    });
  });
});
