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
  detailed: true,
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
  files: [file('a'), file('b', { presupported: true }), file('pic', { mime: 'image/jpeg', importable: false, filename: null, size: null, detailed: false })],
  files_page: 1,
  files_pages: 1,
  files_next_page: null,
  already_imported: {},
  ...over,
});

let listRequests: URL[] = [];
let importRequests: Array<{ model_id: string; file_id: string; folder_id: number | null }> = [];
/** Not the harness's administrator: a user with exactly these permissions. */
function asUser(permissions: string[]) {
  server.use(
    http.get('/api/v1/auth/me', () =>
      HttpResponse.json({
        id: 2,
        username: 'operator',
        role: 'user',
        is_active: true,
        is_admin: false,
        groups: [],
        permissions,
        created_at: '2024-01-01T00:00:00Z',
      }),
    ),
  );
}

/** What the mocked library holds after the imports of this test, by model id and file id. */
let landed: Record<string, Record<string, { library_file_id: number; filename: string; folder_id: number }>> = {};
/** An import of this file waits until `release()` — a run caught in flight. */
let held: { fileId: string; gate: Promise<void> } | null = null;
let release: () => void = () => {};
function hold(fileId: string) {
  held = { fileId, gate: new Promise<void>((resolve) => { release = resolve; }) };
}

function connected(usable = true) {
  server.use(
    http.get(`${BASE}/status`, () =>
      HttpResponse.json({
        source: 'manyfold',
        name: 'Manyfold',
        configured: usable,
        usable,
        base_url: usable ? 'http://manyfold.lan:3214' : null,
        imports: { active: 0, waiting: 0, limit: 2 },
      }),
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
    // Like the server, a model read names the files the library holds — also the ones just imported.
    http.get(`${BASE}/models/:id`, ({ params }) =>
      params.id === 'm2'
        ? HttpResponse.json(model({ id: 'm2', name: 'Spool Holder', files: [file('c')], already_imported: landed.m2 ?? {} }))
        : HttpResponse.json(model({ already_imported: landed.m1 ?? {} })),
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
      const row = { library_file_id: importRequests.length, filename: `${body.file_id}.stl`, folder_id: 7 };
      landed[body.model_id] = { ...landed[body.model_id], [body.file_id]: row };
      return HttpResponse.json({ ...row, was_existing: body.file_id === 'c' });
    }),
  );
}

beforeEach(() => {
  listRequests = [];
  importRequests = [];
  held = null;
  landed = {};
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

  it('with the right to view only, shows the catalogue and no import control', async () => {
    connected();
    asUser(['manyfold:view']);
    let folderRequests = 0;
    server.use(
      http.get('/api/v1/library/folders', () => {
        folderRequests += 1;
        return HttpResponse.json([]);
      }),
    );
    render(<ManyfoldTab />);
    await screen.findByText('Spool Holder');
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('Cable Clip'));
    await screen.findByText('a.stl');
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Import/ })).not.toBeInTheDocument();
    expect(screen.queryByText('Import to')).not.toBeInTheDocument();
    expect(folderRequests).toBe(0);
  });

  it('offers no import without the right to add files to the library', async () => {
    connected();
    asUser(['manyfold:view', 'manyfold:import']); // the server asks library:upload too
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));
    await screen.findByText('a.stl');

    expect(screen.queryByRole('button', { name: /Import/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  });

  it('keeps the ticked models and their import bar when a search finds nothing', async () => {
    connected();
    server.use(
      http.get(`${BASE}/models`, ({ request }) => {
        const q = new URL(request.url).searchParams.get('q');
        return HttpResponse.json({
          total: q ? 0 : 2,
          page: 1,
          pages: 1,
          has_next: false,
          has_previous: false,
          models: q ? [] : [{ id: 'm1', name: 'Cable Clip' }, { id: 'm2', name: 'Spool Holder' }],
        });
      }),
    );
    render(<ManyfoldTab />);
    await screen.findByText('Spool Holder');
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select page' }));

    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'nothing like it' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await screen.findByText('No models found.');

    expect(screen.getByText('Models selected: 2')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Import their files' })).toBeEnabled();
    expect(screen.queryByRole('checkbox', { name: 'Select page' })).not.toBeInTheDocument(); // nothing on the page to tick
    fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }));
    expect(screen.queryByText('Models selected: 2')).not.toBeInTheDocument();
  });

  it('loads file details a page at a time, and asks the same page again when it came back incomplete', async () => {
    connected();
    const pages: string[] = [];
    let firstPageReads = 0;
    const waiting = (id: string, name: string) => file(id, { name, filename: null, size: null, presupported: null, detailed: false });
    server.use(
      http.get(`${BASE}/models/:id`, ({ request }) => {
        const page = new URL(request.url).searchParams.get('files_page');
        expect(new URL(request.url).searchParams.has('detail_cursor')).toBe(false);
        if (page === null) {
          firstPageReads += 1;
          // Every file is listed; the names and sizes of the first page only.
          return HttpResponse.json(
            model({ files: [file('a'), waiting('y', 'Hook'), waiting('z', 'Bracket')], files_page: 1, files_pages: 2, files_next_page: 2 }),
          );
        }
        pages.push(page);
        // The second page, cut short by the deadline the first time it is asked.
        const whole = pages.length > 1;
        return HttpResponse.json(
          model({
            // A page does not repeat the details of another one: "a" comes undetailed here.
            files: [
              file('a', { filename: null, size: null, detailed: false }),
              file('y', { name: 'Hook', filename: 'hook.stl', size: 1024 }),
              whole ? file('z', { name: 'Bracket', filename: 'bracket.stl', size: 4096 }) : waiting('z', 'Bracket'),
            ],
            files_page: 2,
            files_pages: 2,
            files_next_page: null,
          }),
        );
      }),
    );
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));

    // A file whose details were not read yet is listed by its title, without a size, and says so.
    const pending = (await screen.findByText('Bracket')).closest('li') as HTMLElement;
    expect(within(pending).getByText(/details not loaded yet/)).toBeInTheDocument();
    expect(within(pending).queryByText(/KB/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Load more file details' }));

    expect(await screen.findByText('hook.stl')).toBeInTheDocument();
    expect(pages).toEqual(['2']);
    expect(screen.getByText('a.stl')).toBeInTheDocument(); // a detailed file is never replaced by an undetailed one
    // The page came back with a file still undetailed: the same page can be asked again.
    expect(within(screen.getByText('Bracket').closest('li') as HTMLElement).getByText(/details not loaded yet/)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: 'Load more file details' }));

    expect(await screen.findByText('bracket.stl')).toBeInTheDocument();
    expect(pages).toEqual(['2', '2']);
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Load more file details' })).not.toBeInTheDocument());
    expect(screen.queryByText(/details not loaded yet/)).not.toBeInTheDocument();

    // An import re-reads the model; the details already loaded are not thrown away.
    fireEvent.click(within(screen.getByText('a.stl').closest('li') as HTMLElement).getByRole('button', { name: 'Import' }));
    await waitFor(() => expect(firstPageReads).toBe(2));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Import all' })).toBeEnabled());
    expect(screen.getByText('bracket.stl')).toBeInTheDocument();
    expect(screen.getByText('hook.stl')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more file details' })).not.toBeInTheDocument();
  });

  it('stops offering more details when asking again brings nothing new', async () => {
    connected();
    let reads = 0;
    server.use(
      http.get(`${BASE}/models/:id`, () => {
        reads += 1;
        return HttpResponse.json(
          model({ files: [file('a'), file('z', { name: 'Bracket', filename: null, size: null, detailed: false })] }),
        );
      }),
    );
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));
    await screen.findByText('Bracket');

    // One page, one file Manyfold never details: asked again twice, then the control goes.
    fireEvent.click(screen.getByRole('button', { name: 'Load more file details' }));
    await waitFor(() => expect(reads).toBe(2));
    fireEvent.click(await screen.findByRole('button', { name: 'Load more file details' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Load more file details' })).not.toBeInTheDocument());
    expect(reads).toBe(3);
    // The file is still offered: an import reads its own details.
    expect(within(screen.getByText('Bracket').closest('li') as HTMLElement).getByRole('button', { name: 'Import' })).toBeEnabled();
  });

  it('a model whose answer carries every detail offers no "load more"', async () => {
    connected();
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));
    await screen.findByText('a.stl');
    expect(screen.queryByRole('button', { name: 'Load more file details' })).not.toBeInTheDocument();
    expect(screen.queryByText(/details not loaded yet/)).not.toBeInTheDocument();
  });

  it('marks a sensitive model and keeps its preview blurred until it is asked for', async () => {
    connected();
    server.use(http.get(`${BASE}/models/:id`, () => HttpResponse.json(model({ sensitive: true, has_preview: true }))));
    const { container } = render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));

    expect(await screen.findByText('Sensitive')).toBeInTheDocument();
    const picture = () => container.querySelector('img[src*="/models/m1/preview"]') as HTMLImageElement;
    await waitFor(() => expect(picture()).not.toBeNull());
    expect(picture().className).toContain('blur');
    fireEvent.click(screen.getByRole('button', { name: 'Show the preview' }));
    expect(picture().className).not.toContain('blur');
  });

  it('shows previews through our own proxy, and a neutral tile when one fails', async () => {
    connected();
    const { container } = render(<ManyfoldTab />);
    await screen.findByText('Spool Holder');

    const pictures = [...container.querySelectorAll('img')];
    expect(pictures).toHaveLength(2);
    for (const picture of pictures) {
      expect(picture.getAttribute('src')).toMatch(/^\/api\/v1\/model-sources\/manyfold\/models\/m[12]\/preview/);
      expect(picture).toHaveAttribute('alt', ''); // the name is the tile's text; not read twice
    }
    fireEvent.error(pictures[0]);
    expect(container.querySelectorAll('img')).toHaveLength(1);
  });

  it('links to Manyfold only through an http(s) address, in a new tab without an opener', async () => {
    connected();
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));
    const link = await screen.findByRole('link', { name: /Open in Manyfold/ });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');

    server.use(http.get(`${BASE}/models/:id`, () => HttpResponse.json(model({ id: 'm2', url: 'javascript:alert(1)' }))));
    fireEvent.click(screen.getByRole('button', { name: 'Back to models' }));
    fireEvent.click(await screen.findByText('Spool Holder'));
    await screen.findByText('Jane Doe');
    expect(screen.queryByRole('link', { name: /Open in Manyfold/ })).not.toBeInTheDocument();
  });

  it('says a failed status read could not be checked, not that Manyfold is not connected', async () => {
    connected();
    let failing = true;
    server.use(
      http.get(`${BASE}/status`, () =>
        failing
          ? HttpResponse.json({ detail: 'Internal Server Error' }, { status: 500 })
          : HttpResponse.json({ source: 'manyfold', name: 'Manyfold', configured: true, usable: true, base_url: null }),
      ),
    );
    render(<ManyfoldTab />);

    expect(await screen.findByText('BamDude could not check the Manyfold connection.')).toBeInTheDocument();
    expect(screen.queryByText('Manyfold is not connected')).not.toBeInTheDocument();
    failing = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Spool Holder')).toBeInTheDocument();
  });

  it('keeps ticks across pages, names a model it could not read, and clears the selection after the run', async () => {
    connected();
    server.use(
      http.get(`${BASE}/models/:id`, ({ params }) =>
        params.id === 'm3'
          ? HttpResponse.json({ detail: { error: 'manyfold_not_found', message: 'gone' } }, { status: 404 })
          : HttpResponse.json(model({ files: [file('a')] })),
      ),
    );
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select Cable Clip' }));
    fireEvent.click(screen.getByRole('button', { name: /Next/ }));
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select Third' }));
    expect(screen.getByText('Models selected: 2')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Import their files' }));
    expect(await screen.findByText('Imported: 1 · already in library: 0 · failed: 0')).toBeInTheDocument();
    expect(screen.getByText('Could not read: Third')).toBeInTheDocument();
    expect(importRequests.map((request) => `${request.model_id}/${request.file_id}`)).toEqual(['m1/a']);
    expect(screen.queryByText(/Models selected/)).not.toBeInTheDocument();
  });

  it('tells "nothing printable" from "everything is already in the library"', async () => {
    connected();
    server.use(
      http.get(`${BASE}/models/:id`, () =>
        HttpResponse.json(model({ files: [file('pic', { mime: 'image/jpeg', importable: false })] })),
      ),
    );
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select Cable Clip' }));
    fireEvent.click(screen.getByRole('button', { name: 'Import their files' }));
    expect(await screen.findByText(/these models hold no file BamDude can print/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));

    server.use(
      http.get(`${BASE}/models/:id`, () =>
        HttpResponse.json(model({ files: [file('a')], already_imported: { a: { library_file_id: 5, folder_id: null, filename: 'a.stl' } } })),
      ),
    );
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Cable Clip' }));
    fireEvent.click(screen.getByRole('button', { name: 'Import their files' }));
    expect(await screen.findByText(/every printable file is already in your library/)).toBeInTheDocument();
    expect(importRequests).toHaveLength(0);
  });

  it('shows the server\'s own sentence for a refusal code this build does not know', async () => {
    connected();
    server.use(
      http.post(`${BASE}/import`, () =>
        HttpResponse.json(
          { detail: { error: 'a_code_from_a_newer_server', message: 'Imports are busy. Try again in a moment.' } },
          { status: 503 },
        ),
      ),
    );
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));
    fireEvent.click(
      within((await screen.findByText('a.stl')).closest('li') as HTMLElement).getByRole('button', { name: 'Import' }),
    );

    expect(
      await within(screen.getByTestId('toast-viewport')).findByText('Imports are busy. Try again in a moment.', {}, { timeout: 5000 }),
    ).toBeInTheDocument();
  });

  it('a failed page is not a dead end: Retry, and the pager stays', async () => {
    connected();
    let failing = true;
    server.use(
      http.get(`${BASE}/models`, ({ request }) => {
        const page = Number(new URL(request.url).searchParams.get('page') ?? '1');
        if (page === 2 && failing) {
          return HttpResponse.json({ detail: { error: 'manyfold_rate_limited', message: 'Slow down' } }, { status: 429 });
        }
        return HttpResponse.json({
          total: 3,
          page,
          pages: 2,
          has_next: page === 1,
          has_previous: page > 1,
          models: page === 1 ? [{ id: 'm1', name: 'Cable Clip' }] : [{ id: 'm3', name: 'Third' }],
        });
      }),
    );
    render(<ManyfoldTab />);
    await screen.findByText('Cable Clip');
    fireEvent.click(screen.getByRole('button', { name: /Next/ }));

    expect(await screen.findByText('Manyfold is limiting requests. Try again in a few minutes.')).toBeInTheDocument();
    // The way back is still there, and the way forward is not offered past the last known page.
    expect(screen.getByRole('button', { name: /Previous/ })).toBeEnabled();
    expect(screen.getByRole('button', { name: /Next/ })).toBeDisabled();
    expect(screen.getByText('Page 2')).toBeInTheDocument();

    failing = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Third')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Previous/ }));
    expect(await screen.findByText('Cable Clip')).toBeInTheDocument();
  });

  it('Search with the same words asks again after a failure', async () => {
    connected();
    let failing = true;
    server.use(
      http.get(`${BASE}/models`, () => {
        listRequests.push(new URL('http://x/'));
        return failing
          ? HttpResponse.json({ detail: { error: 'manyfold_unreachable', message: 'unreachable' } }, { status: 502 })
          : HttpResponse.json({ total: 1, page: 1, pages: 1, has_next: false, has_previous: false, models: [{ id: 'm1', name: 'Cable Clip' }] });
      }),
    );
    render(<ManyfoldTab />);
    await screen.findByText(/BamDude cannot reach Manyfold/);
    expect(listRequests).toHaveLength(1);

    failing = false;
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    expect(await screen.findByText('Cable Clip')).toBeInTheDocument();
    expect(listRequests).toHaveLength(2);
  });

  it('a model that could not be read offers Retry', async () => {
    connected();
    let failing = true;
    server.use(
      http.get(`${BASE}/models/:id`, () =>
        failing
          ? HttpResponse.json({ detail: { error: 'manyfold_unreachable', message: 'unreachable' } }, { status: 502 })
          : HttpResponse.json(model()),
      ),
    );
    render(<ManyfoldTab />);
    fireEvent.click(await screen.findByText('Cable Clip'));
    await screen.findByText(/BamDude cannot reach Manyfold/);

    failing = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Jane Doe')).toBeInTheDocument();
  });

  it('keeps the place in the catalogue in the address, so Back returns to it', async () => {
    connected();
    window.history.replaceState({}, '', '/model-sources?tab=manyfold');
    render(<ManyfoldTab />);
    await screen.findByText('Spool Holder');
    const params = () => new URLSearchParams(window.location.search);

    fireEvent.click(screen.getByRole('button', { name: /Next/ }));
    await screen.findByText('Third');
    expect(params().get('page')).toBe('2');
    expect(params().get('tab')).toBe('manyfold'); // the tab's own parameter stays

    // Typing is not a place: the address changes when the search is submitted.
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'clip' } });
    expect(params().get('q')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await screen.findByText('Models: 1');
    expect(params().get('q')).toBe('clip');
    expect(params().get('page')).toBeNull(); // a new search starts at the first page

    const before = window.history.length;
    fireEvent.click(screen.getByText('Cable Clip'));
    await screen.findByText('Jane Doe');
    expect(params().get('model')).toBe('m1');
    expect(window.history.length).toBe(before + 1); // opening a model is a step Back can undo

    window.history.back();
    // Back from the model: the same search, not the page before Model sources.
    expect(await screen.findByText('Models: 1')).toBeInTheDocument();
    expect(params().get('model')).toBeNull();
    expect(params().get('q')).toBe('clip');
    expect(screen.getByRole('searchbox')).toHaveValue('clip');

    window.history.back();
    // …and once more: page 2 of the unfiltered list, with the search box following the address.
    expect(await screen.findByText('Third')).toBeInTheDocument();
    expect(screen.getByRole('searchbox')).toHaveValue('');
  });

  it('opens the page, the search and the model an address names (a reload, a shared link)', async () => {
    connected();
    window.history.replaceState({}, '', '/model-sources?tab=manyfold&page=2');
    const first = render(<ManyfoldTab />);
    expect(await screen.findByText('Third')).toBeInTheDocument();
    expect(listRequests[listRequests.length - 1].searchParams.get('page')).toBe('2');
    first.unmount();

    window.history.replaceState({}, '', '/model-sources?tab=manyfold&q=clip&model=m2');
    render(<ManyfoldTab />);
    expect(await screen.findByRole('heading', { name: 'Spool Holder' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Back to models' }));
    expect(await screen.findByText('Models: 1')).toBeInTheDocument(); // the search the address named
    expect(screen.getByRole('searchbox')).toHaveValue('clip');
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
