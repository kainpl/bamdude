/**
 * The Manyfold tab: the catalogue is the server's (its pages, its search), a model shows its files,
 * and several files import one after another with a summary — a failure never stops the rest.
 */

import { useState, type ReactNode } from 'react';
import { describe, it, expect, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render as renderBare } from '../utils';
import { ManyfoldTab } from '../../components/ManyfoldTab';
import { ManyfoldImportProvider } from '../../contexts/ManyfoldImportContext';

/** The run's owner sits above the page in the app (App.tsx); here above the tab. */
const render = (ui: ReactNode) => renderBare(<ManyfoldImportProvider>{ui}</ManyfoldImportProvider>);

/** The tab as a page the user can leave and come back to, under one owner. */
function LeavablePage() {
  const [shown, setShown] = useState(true);
  return (
    <>
      <button type="button" onClick={() => setShown(!shown)}>{shown ? 'leave' : 'return'}</button>
      {shown && <ManyfoldTab />}
    </>
  );
}

const BASE = '/api/v1/model-sources/manyfold';

const file = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  name: 'Clip',
  mime: 'model/stl',
  importable: true,
  filename: `${id}.stl`,
  size: 2048,
  presupported: false,
  available: true,
  ...over,
});

const model = (over: Record<string, unknown> = {}) => ({
  id: 'm1',
  name: 'Cable Clip',
  caption: 'Clip for cables',
  description: null,
  license: 'CC-BY-4.0',
  creator: 'Jane Doe',
  tags: ['clip'],
  sensitive: false,
  url: 'http://manyfold.lan:3214/models/m1',
  has_preview: false,
  files: [file('a'), file('b', { presupported: true }), file('pic', { mime: 'image/jpeg', importable: false, filename: null, size: null })],
  already_imported: {},
  ...over,
});

let listRequests: URL[] = [];
let importRequests: Array<{ model_id: string; file_id: string; folder_id: number | null }> = [];
/** An import of this file waits until `release()` — a run caught in flight. */
let held: { fileId: string; gate: Promise<void> } | null = null;
let release: () => void = () => {};
function hold(fileId: string) {
  held = { fileId, gate: new Promise<void>((resolve) => { release = resolve; }) };
}

function connected(usable = true) {
  server.use(
    http.get(`${BASE}/status`, () =>
      HttpResponse.json({ source: 'manyfold', name: 'Manyfold', configured: usable, usable, base_url: usable ? 'http://manyfold.lan:3214' : null }),
    ),
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
    http.get(`${BASE}/models`, ({ request }) => {
      const url = new URL(request.url);
      listRequests.push(url);
      const page = Number(url.searchParams.get('page') ?? '1');
      const q = url.searchParams.get('q');
      const models = q
        ? [{ id: 'm1', name: 'Cable Clip' }]
        : page === 1
          ? [{ id: 'm1', name: 'Cable Clip' }, { id: 'm2', name: 'Spool Holder' }]
          : [{ id: 'm3', name: 'Third' }];
      return HttpResponse.json({ total: q ? 1 : 3, page, pages: q ? 1 : 2, has_next: !q && page === 1, has_previous: page > 1, models });
    }),
    http.get(`${BASE}/models/:id`, ({ params }) =>
      params.id === 'm2'
        ? HttpResponse.json(model({ id: 'm2', name: 'Spool Holder', files: [file('c')] }))
        : HttpResponse.json(model()),
    ),
    http.post(`${BASE}/import`, async ({ request }) => {
      const body = (await request.json()) as { model_id: string; file_id: string; folder_id: number | null };
      importRequests.push(body);
      if (held && held.fileId === body.file_id) await held.gate;
      if (body.file_id === 'b') {
        return HttpResponse.json(
          { detail: { error: 'manyfold_too_large', message: 'The file is larger than BamDude accepts from Manyfold' } },
          { status: 413 },
        );
      }
      return HttpResponse.json({
        library_file_id: importRequests.length,
        filename: `${body.file_id}.stl`,
        folder_id: 7,
        was_existing: body.file_id === 'c',
      });
    }),
  );
}

beforeEach(() => {
  listRequests = [];
  importRequests = [];
  held = null;
  window.sessionStorage.clear();
  window.history.replaceState({}, '', '/');
});

describe('ManyfoldTab', () => {
  it('says Manyfold is not connected and where to connect it', async () => {
    connected(false);
    render(<ManyfoldTab />);

    expect(await screen.findByText('Manyfold is not connected')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Connection settings/ })).toHaveAttribute('href', '/settings?tab=network');
    expect(listRequests).toHaveLength(0);
  });

  it('pages and searches on the server, never in the loaded page', async () => {
    connected();
    render(<ManyfoldTab />);

    expect(await screen.findByText('Spool Holder')).toBeInTheDocument();
    expect(screen.getByText('Models: 3')).toBeInTheDocument();
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Next/ }));
    expect(await screen.findByText('Third')).toBeInTheDocument();
    expect(screen.queryByText('Spool Holder')).not.toBeInTheDocument();

    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'clip' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() => expect(screen.getByText('Models: 1')).toBeInTheDocument());

    const last = listRequests[listRequests.length - 1];
    expect(last.searchParams.get('q')).toBe('clip');
    expect(last.searchParams.get('page')).toBe('1'); // a new search starts at the first page
    expect([...last.searchParams.keys()].sort()).toEqual(['page', 'q']); // no order, no size: they are the source's
  });

  it('shows a model with its origin and which files can be imported', async () => {
    connected();
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));

    expect(await screen.findByText('Jane Doe')).toBeInTheDocument();
    expect(screen.getByText('CC-BY-4.0')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Open in Manyfold/ })).toHaveAttribute('href', 'http://manyfold.lan:3214/models/m1');
    expect(screen.getByText('a.stl')).toBeInTheDocument();
    expect(screen.getByText(/with supports/)).toBeInTheDocument();
    // The picture is listed, cannot be ticked and has no Import button.
    const picture = screen.getByText('Clip').closest('li') as HTMLElement;
    expect(within(picture).getByText(/not imported/)).toBeInTheDocument();
    expect(within(picture).queryByRole('checkbox')).not.toBeInTheDocument();
    expect(within(picture).queryByRole('button')).not.toBeInTheDocument();
  });

  it('imports the selected files one after another and reports what failed', async () => {
    connected();
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));
    await screen.findByText('a.stl');

    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all' }));
    fireEvent.click(screen.getByRole('button', { name: 'Import selected (2)' }));

    const summary = await screen.findByText('Imported: 1 · already in library: 0 · failed: 1');
    expect(summary).toBeInTheDocument();
    expect(screen.getByText(/Not imported: Cable Clip — b\.stl\. The file is larger than 1 GB\./)).toBeInTheDocument();
    expect(importRequests.map((request) => request.file_id)).toEqual(['a', 'b']); // the failure did not stop at "a"… nor before "b"
    // The imported file now reads "In library" with a way to it.
    const imported = screen.getByText('a.stl').closest('li') as HTMLElement;
    expect(within(imported).getByText('In library')).toBeInTheDocument();
    expect(within(imported).getByRole('link', { name: 'Show in library' })).toHaveAttribute('href', '/files?folder=7');
  });

  it('marks what the library already holds and does not offer it again', async () => {
    connected();
    server.use(
      http.get(`${BASE}/models/:id`, () =>
        HttpResponse.json(model({ already_imported: { a: { library_file_id: 5, folder_id: null, filename: 'a.stl' } } })),
      ),
    );
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));

    const held = (await screen.findByText('a.stl')).closest('li') as HTMLElement;
    expect(within(held).getByText('In library')).toBeInTheDocument();
    expect(within(held).queryByRole('checkbox')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Import all' }));
    await screen.findByText(/Imported: 0 · already in library: 0 · failed: 1/);
    expect(importRequests.map((request) => request.file_id)).toEqual(['b']);
  });

  it('imports every printable file of the models ticked in the grid', async () => {
    connected();
    render(<ManyfoldTab />);
    await screen.findByText('Spool Holder');

    fireEvent.click(screen.getByRole('checkbox', { name: 'Select page' }));
    expect(screen.getByText('Models selected: 2')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Import their files' }));

    await screen.findByText('Imported: 1 · already in library: 1 · failed: 1');
    expect(importRequests.map((request) => `${request.model_id}/${request.file_id}`)).toEqual(['m1/a', 'm1/b', 'm2/c']);
  });

  it('says in words why Manyfold refused the list', async () => {
    connected();
    server.use(
      http.get(`${BASE}/models`, () =>
        HttpResponse.json({ detail: { error: 'manyfold_too_old', message: 'This Manyfold is too old (needs 0.150 or newer).' } }, { status: 502 }),
      ),
    );
    render(<ManyfoldTab />);

    expect(await screen.findByText('This Manyfold is too old. BamDude needs Manyfold 0.150 or newer.')).toBeInTheDocument();
  });

  it('an import goes on when the tab is left, and shows its progress and outcome on return', async () => {
    connected();
    hold('b');
    render(<LeavablePage />);
    fireEvent.click(await screen.findByText('Cable Clip'));
    await screen.findByText('a.stl');
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select all' }));
    fireEvent.click(screen.getByRole('button', { name: 'Import selected (2)' }));
    await waitFor(() => expect(importRequests).toHaveLength(2));

    fireEvent.click(screen.getByRole('button', { name: 'leave' }));
    // Elsewhere in the app a compact indicator says an import is running.
    expect(within(screen.getByTestId('toast-viewport')).getByText(/Manyfold import: 2\/2/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'return' }));
    expect(await screen.findByText('Importing 2/2')).toBeInTheDocument();
    // A second run cannot be started on top of it, and the control says why.
    fireEvent.click(await screen.findByText('Cable Clip'));
    const again = await screen.findByRole('button', { name: 'Import all' });
    expect(again).toBeDisabled();
    expect(again).toHaveAttribute('title', 'An import is running. Wait for it to finish, or cancel it.');

    release();
    expect(await screen.findByText('Imported: 1 · already in library: 0 · failed: 1')).toBeInTheDocument();
    expect(screen.getByText(/Not imported: Cable Clip — b\.stl\. The file is larger than 1 GB\./)).toBeInTheDocument();
    expect(importRequests.map((request) => request.file_id)).toEqual(['a', 'b']);
    // The summary stays until it is dismissed.
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByText('Imported: 1 · already in library: 0 · failed: 1')).not.toBeInTheDocument();
  });

  it('Cancel stops the run after the file in flight', async () => {
    connected();
    hold('a');
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));
    await screen.findByText('a.stl');
    fireEvent.click(screen.getByRole('button', { name: 'Import all' }));
    await waitFor(() => expect(importRequests).toHaveLength(1));

    fireEvent.click(screen.getByRole('button', { name: 'Cancel import' }));
    expect(await screen.findByText(/Stopping after the file being imported/)).toBeInTheDocument();
    release();

    expect(await screen.findByText('Imported: 1 · already in library: 0 · failed: 0')).toBeInTheDocument();
    expect(screen.getByText(/Stopped at your request\. Files not tried: 1\./)).toBeInTheDocument();
    expect(importRequests.map((request) => request.file_id)).toEqual(['a']); // "b" was never asked for
  });

  it('a refusal no other file can get past stops the run instead of repeating itself', async () => {
    connected();
    server.use(
      http.post(`${BASE}/import`, async ({ request }) => {
        importRequests.push((await request.json()) as { model_id: string; file_id: string; folder_id: number | null });
        return HttpResponse.json({ detail: { error: 'manyfold_disabled', message: 'Manyfold is switched off' } }, { status: 409 });
      }),
    );
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));
    await screen.findByText('a.stl');
    fireEvent.click(screen.getByRole('button', { name: 'Import all' }));

    expect(
      await screen.findByText(/The import stopped: Manyfold is switched off in Settings\. Files not tried: 1\./),
    ).toBeInTheDocument();
    expect(importRequests).toHaveLength(1);
  });

  it('after a reload says what the interrupted import had done', async () => {
    connected();
    window.sessionStorage.setItem(
      'bamdude.manyfoldImport',
      JSON.stringify({
        done: 3,
        total: 40,
        imported: 2,
        existing: 0,
        failed: [{ key: 'm1:b', modelName: 'Cable Clip', name: 'b.stl', reason: 'The file is larger than 1 GB.' }],
        unreadable: [],
      }),
    );
    render(<ManyfoldTab />);

    expect(
      await screen.findByText(/The page was reloaded during an import: 3 of 40 files had been handled\./),
    ).toBeInTheDocument();
    expect(screen.getByText('Imported: 2 · already in library: 0 · failed: 1')).toBeInTheDocument();
    expect(screen.getByText(/Not imported: Cable Clip — b\.stl/)).toBeInTheDocument();
    expect(window.sessionStorage.getItem('bamdude.manyfoldImport')).toBeNull();
  });

  it('a single file reports with a toast: imported, or the refusal by its code', async () => {
    connected();
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));
    const toasts = within(screen.getByTestId('toast-viewport'));

    fireEvent.click(
      within((await screen.findByText('a.stl')).closest('li') as HTMLElement).getByRole('button', { name: 'Import' }),
    );
    expect(await toasts.findByText('Imported a.stl')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Import all' })).toBeEnabled());

    fireEvent.click(within(screen.getByText('b.stl').closest('li') as HTMLElement).getByRole('button', { name: 'Import' }));
    expect(await toasts.findByText('The file is larger than 1 GB.')).toBeInTheDocument();
  });
});
