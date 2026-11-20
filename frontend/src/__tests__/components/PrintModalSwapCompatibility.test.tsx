/**
 * The print dialog reads a source's swap declaration from ONE active read (swap-source-lifecycle
 * spec r4 §9): a library file or an archive its own live row, a copied or an edited queue job its
 * own copy source — never an OR across caches, and never a stale answer another source left behind.
 *
 * - True: BamDude adds none of its swap macros; the panel stays shut and the payload says so.
 * - False: the operator's swap choices apply as before.
 * - Unknown (and a read in flight or one that failed): a neutral note; while our swap macros are
 *   requested the job cannot be sent, and the one way on is an explicit opt-out for THIS job.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor, render as rtlRender } from '@testing-library/react';
import { http, HttpResponse, delay } from 'msw';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router';
import { server } from '../mocks/server';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { AuthProvider } from '../../contexts/AuthContext';
import { ToastProvider } from '../../contexts/ToastContext';
import type { PrintQueueItem } from '../../api/client';

const SWAP_PRINTER = {
  id: 1,
  name: 'P1S-swap',
  model: 'P1S',
  ip_address: '192.168.1.101',
  enabled: true,
  is_active: true,
  swap_mode_enabled: true,
};

type Fact = { swap_compatible: boolean; swap_compatibility_known: boolean };
const TRUE: Fact = { swap_compatible: true, swap_compatibility_known: true };
const FALSE: Fact = { swap_compatible: false, swap_compatibility_known: true };
const UNKNOWN: Fact = { swap_compatible: false, swap_compatibility_known: false };

let posted: Array<Record<string, unknown>>;

/** An archive answers its executed run's pair AND its source's (spec r3 §7); a reprint reads the source's. */
function archiveAnswer(source: Fact, run: Fact = source) {
  return {
    ...run,
    source_swap_compatible: source.swap_compatible,
    source_swap_compatibility_known: source.swap_compatibility_known,
  };
}

function sources({
  archive,
  archiveRun,
  library,
  copy,
}: {
  archive?: Fact | 'error' | 'slow';
  archiveRun?: Fact;
  library?: Fact | 'error';
  copy?: Fact | 'error';
}) {
  server.use(
    http.get('/api/v1/archives/:id', async () => {
      if (archive === 'error') return HttpResponse.json({ detail: 'boom' }, { status: 500 });
      if (archive === 'slow') {
        await delay(300);
        return HttpResponse.json({ id: 1, filename: 'a.3mf', sliced_for_model: null, ...archiveAnswer(TRUE) });
      }
      return HttpResponse.json({
        id: 1,
        filename: 'a.3mf',
        sliced_for_model: null,
        ...archiveAnswer(archive ?? FALSE, archiveRun),
      });
    }),
    http.get('/api/v1/library/files/:id', () => {
      if (library === 'error') return HttpResponse.json({ detail: 'boom' }, { status: 500 });
      return HttpResponse.json({ id: 1, filename: 'l.3mf', sliced_for_model: null, ...(library ?? FALSE) });
    }),
    http.get('/api/v1/library/files/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
    http.get('/api/v1/library/files/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
    http.get('/api/v1/queue/:id/copy-source', () => {
      if (copy === 'error') return HttpResponse.json({ detail: { code: 'source_unreadable', message: 'x' } }, { status: 422 });
      return HttpResponse.json({
        item_id: 9,
        filename: 'c.3mf',
        sliced_for_model: null,
        plates: [],
        is_multi_plate: false,
        ...(copy ?? FALSE),
      });
    }),
  );
}

beforeEach(() => {
  localStorage.clear();
  posted = [];
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([SWAP_PRINTER])),
    http.get('/api/v1/printers/:id/status', () =>
      HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [] }),
    ),
    http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
    http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
    http.get('/api/v1/print-option-preferences/:model', () => HttpResponse.json(null)),
    http.put('/api/v1/print-option-preferences/:model', async ({ request }) =>
      HttpResponse.json({ printer_model: 'P1S', options: await request.json(), updated_at: '' }),
    ),
    http.post('/api/v1/queue/', async ({ request }) => {
      posted.push((await request.json()) as Record<string, unknown>);
      return HttpResponse.json({ id: 1, status: 'pending', created_item_ids: [1] });
    }),
    http.patch('/api/v1/queue/:id', async ({ request }) => {
      posted.push((await request.json()) as Record<string, unknown>);
      return HttpResponse.json({ id: 9, status: 'pending' });
    }),
  );
});

const addToQueue = (props: Partial<React.ComponentProps<typeof PrintModal>>) =>
  render(
    <PrintModal
      mode="add-to-queue"
      archiveName="Bracket"
      initialSelectedPrinterIds={[1]}
      onClose={vi.fn()}
      onSuccess={vi.fn()}
      {...props}
    />,
  );

const submitButton = () => screen.findByRole('button', { name: /^add to queue$/i });

async function submit() {
  const button = await submitButton();
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  await waitFor(() => expect(posted).toHaveLength(1));
  return posted[0];
}

describe('the swap declaration of the source being printed', () => {
  it("an archive is reprinted by its SOURCE's declaration, never by what its run was", async () => {
    sources({ archive: UNKNOWN, archiveRun: TRUE });
    addToQueue({ archiveId: 1 });

    expect(await screen.findByText(/swap compatibility of this source is unknown/i)).toBeInTheDocument();
  });

  it('a saved declaration is read again by the open dialog, and the saved swap settings stay as they were', async () => {
    let declared = false;
    let preferenceWrites = 0;
    server.use(
      http.get('/api/v1/archives/:id', () =>
        HttpResponse.json({ id: 1, filename: 'a.3mf', sliced_for_model: null, ...archiveAnswer(declared ? TRUE : UNKNOWN) }),
      ),
      http.put('/api/v1/print-option-preferences/:model', () => {
        preferenceWrites += 1;
        return HttpResponse.json({});
      }),
      http.post('/api/v1/archives/:id/swap-declaration/inspect', () =>
        HttpResponse.json({
          kind: 'archive',
          id: 1,
          sha256: 'ab'.repeat(32),
          size_bytes: 10,
          display_filename: 'a.3mf',
          current: null,
          known: false,
          revision: 'cd'.repeat(32),
          origin: null,
          declaration_id: null,
        }),
      ),
      http.put('/api/v1/archives/:id/swap-declaration', () => {
        declared = true;
        return HttpResponse.json({
          kind: 'archive',
          id: 1,
          declaration_id: '1b4e28ba-2fa1-41d2-883f-0016d3cca427',
          value: true,
          sha256: 'ab'.repeat(32),
          revision: 'ef'.repeat(32),
          replayed: false,
        });
      }),
      http.get('/api/v1/archives/:id/swap-declaration/history', () => HttpResponse.json([])),
      http.get('/api/v1/archives/:id/swap-declaration/jobs', () =>
        HttpResponse.json({
          kind: 'archive',
          id: 1,
          declaration_id: '1b4e28ba-2fa1-41d2-883f-0016d3cca427',
          value: true,
          sha256: 'ab'.repeat(32),
          source_revision: 'ef'.repeat(32),
          jobs: [],
          counts: { eligible: 0, refused: 0 },
        }),
      ),
    );
    addToQueue({ archiveId: 1 });

    fireEvent.click(await screen.findByRole('button', { name: /declare this source/i }));
    fireEvent.click(await screen.findByRole('radio', { name: /carries its own plate-change sequence/i }));
    fireEvent.click(screen.getByRole('button', { name: /save declaration/i }));
    await screen.findByText(/declaration saved/i);

    await waitFor(() => expect(screen.queryByText(/swap compatibility of this source is unknown/i)).toBeNull());
    expect(preferenceWrites).toBe(0);
  });

  it('Escape closes the declaration dialog alone, and the print dialog stays', async () => {
    sources({ archive: UNKNOWN });
    server.use(
      http.post('/api/v1/archives/:id/swap-declaration/inspect', () =>
        HttpResponse.json({
          kind: 'archive',
          id: 1,
          sha256: 'ab'.repeat(32),
          size_bytes: 10,
          display_filename: 'a.3mf',
          current: null,
          known: false,
          revision: 'cd'.repeat(32),
          origin: null,
          declaration_id: null,
        }),
      ),
    );
    addToQueue({ archiveId: 1 });
    fireEvent.click(await screen.findByRole('button', { name: /declare this source/i }));
    await screen.findByRole('dialog', { name: /swap declaration/i });

    fireEvent.keyDown(window, { key: 'Escape' });

    await waitFor(() => expect(screen.queryByRole('dialog', { name: /swap declaration/i })).toBeNull());
    expect(await submitButton()).toBeInTheDocument();
  });

  it('the unknown notice offers to declare the source, in its own dialog', async () => {
    sources({ archive: UNKNOWN });
    server.use(
      http.post('/api/v1/archives/:id/swap-declaration/inspect', () =>
        HttpResponse.json({
          kind: 'archive',
          id: 1,
          sha256: 'ab'.repeat(32),
          size_bytes: 10,
          display_filename: 'a.3mf',
          current: null,
          known: false,
          revision: 'cd'.repeat(32),
          origin: null,
          declaration_id: null,
        }),
      ),
    );
    addToQueue({ archiveId: 1 });

    fireEvent.click(await screen.findByRole('button', { name: /declare this source/i }));

    expect(await screen.findByRole('dialog', { name: /swap declaration/i })).toBeInTheDocument();
    expect(await screen.findByRole('radio', { name: /carries its own plate-change sequence/i })).not.toBeChecked();
  });

  it('True: our swap macros are not added and their panel stays shut', async () => {
    sources({ archive: TRUE });
    addToQueue({ archiveId: 1 });

    const body = await submit();
    expect(body).toMatchObject({ execute_swap_macros: false, swap_macro_events: null });
    expect(screen.queryByText('Swap Macros')).toBeNull();
    expect(screen.queryByText(/swap compatibility of this source is unknown/i)).toBeNull();
  });

  it('False: the operator’s swap choices apply', async () => {
    sources({ archive: FALSE });
    addToQueue({ archiveId: 1 });

    expect(await screen.findByText('Swap Macros')).toBeInTheDocument();
    const body = await submit();
    expect(body).toMatchObject({
      execute_swap_macros: true,
      swap_macro_events: ['swap_mode_start', 'swap_mode_change_table'],
    });
  });

  it('Unknown with our swap macros requested: a neutral note, no send until this job opts out', async () => {
    sources({ archive: UNKNOWN });
    addToQueue({ archiveId: 1 });

    expect(await screen.findByText(/swap compatibility of this source is unknown/i)).toBeInTheDocument();
    expect(screen.queryByText(/old/i)).toBeNull();
    expect(screen.queryByText('Swap Macros')).toBeNull();
    const button = await submitButton();
    await waitFor(() => expect(screen.getByText(/cannot be sent as it is/i)).toBeInTheDocument());
    expect(button).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: /print without the extra swap macros/i }));
    expect(screen.getByText(/ordinary plate rules apply/i)).toBeInTheDocument();
    const body = await submit();
    expect(body).toMatchObject({ execute_swap_macros: false, swap_macro_events: null });
  });

  it('Unknown with nothing requested sends as it is', async () => {
    sources({ archive: UNKNOWN });
    server.use(
      http.get('/api/v1/print-option-preferences/:model', () =>
        HttpResponse.json({
          printer_model: 'P1S',
          updated_at: '',
          options: {
            print_options: {},
            swap_macros: { execute: false, events: [] },
          },
        }),
      ),
    );
    addToQueue({ archiveId: 1 });

    expect(await screen.findByText(/swap compatibility of this source is unknown/i)).toBeInTheDocument();
    const body = await submit();
    expect(body).toMatchObject({ execute_swap_macros: false, swap_macro_events: null });
    expect(screen.queryByText(/cannot be sent as it is/i)).toBeNull();
  });

  it('a read in flight is no answer, and neither is one that failed', async () => {
    sources({ archive: 'slow' });
    addToQueue({ archiveId: 1 });
    const button = await submitButton();
    expect(button).toBeDisabled();
    // The read lands True: nothing to refuse any more.
    await waitFor(() => expect(button).toBeEnabled());
  });

  it('a failed read is unknown', async () => {
    sources({ archive: 'error' });
    addToQueue({ archiveId: 1 });

    expect(await screen.findByText(/swap compatibility of this source is unknown/i)).toBeInTheDocument();
    expect(await submitButton()).toBeDisabled();
  });

  it('a library file answers from its own row', async () => {
    sources({ library: TRUE });
    addToQueue({ libraryFileId: 1 });

    const body = await submit();
    expect(body).toMatchObject({ execute_swap_macros: false, swap_macro_events: null });
  });

  it('a copied job answers from its own copy source', async () => {
    sources({ copy: UNKNOWN, archive: TRUE });
    addToQueue({ sourceQueueItemId: 9 });

    expect(await screen.findByText(/swap compatibility of this source is unknown/i)).toBeInTheDocument();
  });
});

describe('one active read, never another source’s cache', () => {
  function seededRender(ui: React.ReactElement, seed: (client: QueryClient) => void) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
    seed(client);
    return rtlRender(
      <QueryClientProvider client={client}>
        <BrowserRouter>
          <ThemeProvider>
            <AuthProvider>
              <ToastProvider>{ui}</ToastProvider>
            </AuthProvider>
          </ThemeProvider>
        </BrowserRouter>
      </QueryClientProvider>,
    );
  }

  it('a library file ignores a True an archive of the same id left in the cache', async () => {
    sources({ library: UNKNOWN });
    seededRender(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={1}
        archiveName="Bracket"
        initialSelectedPrinterIds={[1]}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
      (client) => client.setQueryData(['archive', 1], { id: 1, ...TRUE }),
    );

    expect(await screen.findByText(/swap compatibility of this source is unknown/i)).toBeInTheDocument();
  });

  it('an edited job answers from its own copy source, not the archive it was captured from', async () => {
    sources({ archive: TRUE, copy: UNKNOWN });
    const item = {
      id: 9,
      printer_id: 1,
      queue_id: 1,
      archive_id: 1,
      library_file_id: null,
      plate_id: null,
      status: 'pending',
      execute_swap_macros: true,
      swap_macro_events: ['swap_mode_start', 'swap_mode_change_table'],
      selected_macro_ids: [],
    } as unknown as PrintQueueItem;
    render(
      <PrintModal mode="edit-queue-item" archiveId={1} archiveName="Bracket" queueItem={item} onClose={vi.fn()} />,
    );

    expect(await screen.findByText(/swap compatibility of this source is unknown/i)).toBeInTheDocument();
    expect(await screen.findByText(/cannot be sent as it is/i)).toBeInTheDocument();
  });
});

describe('a silent member of a grouped run', () => {
  it('does not send itself over an unknown source with swap macros requested — it asks', async () => {
    sources({ archive: UNKNOWN });
    const onAutoSubmitRefused = vi.fn();
    addToQueue({ archiveId: 1, autoSubmitWhenUnambiguous: true, onAutoSubmitRefused });

    await waitFor(() => expect(onAutoSubmitRefused).toHaveBeenCalled());
    expect(await screen.findByText(/swap compatibility of this source is unknown/i)).toBeInTheDocument();
    expect(posted).toHaveLength(0);
  });

  it('sends itself over a known source as before', async () => {
    sources({ archive: TRUE });
    addToQueue({ archiveId: 1, autoSubmitWhenUnambiguous: true });

    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({ execute_swap_macros: false, swap_macro_events: null });
  });
});
