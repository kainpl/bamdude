/**
 * "Slice" in the summary of "Assemble project", on the page that owns both
 * dialogs: where it is offered, that the Slice dialog opens OVER the summary
 * (an assembly may have written several files — the others' "Slice" buttons
 * must still be there afterwards), and where the keyboard focus is once the
 * Slice dialog is closed.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import type { Permission } from '../../api/client';
import { SliceJobTrackerProvider } from '../../contexts/SliceJobTrackerContext';
import { FileManagerPage } from '../../pages/FileManagerPage';
import { server } from '../mocks/server';
import { render } from '../utils';

const auth = vi.hoisted(() => ({ granted: new Set<string>() }));

vi.mock('../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({
      ...actual.useAuth(),
      hasPermission: (p: Permission) => auth.granted.has(p),
      hasAnyPermission: (...ps: Permission[]) => ps.some((p) => auth.granted.has(p)),
      canModify: (resource: string, action: string) => auth.granted.has(`${resource}:${action}_all`),
    }),
  };
});

const file = (id: number, name: string) => ({
  id,
  filename: name,
  file_path: `/library/${name}`,
  file_size: 1024,
  file_type: 'stl',
  file_tags: ['stl'],
  plan_eligible: false,
  product_ids: [],
  folder_id: null,
  thumbnail_path: null,
  print_name: null,
  print_time_seconds: null,
  duplicate_count: 0,
  created_at: '2026-11-01T00:00:00Z',
  created_by_id: 5,
});
const FILES = [file(1, 'jar.stl'), file(2, 'lid.stl')];
const EMPTY = { printer: [], process: [], filament: [] };
const local = (id: string, name: string, over: Record<string, unknown> = {}) => ({ id, name, source: 'local', ...over });
const WRITTEN = [
  { library_file_id: 77, name: 'jar +1 — X1C.3mf', plate: 1, fallback_reason: null },
  { library_file_id: 78, name: 'jar +1 — X1C (2).3mf', plate: 2, fallback_reason: null },
];
let recordAsked: string[];

function serve(useSlicerApi: boolean) {
  recordAsked = [];
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
    http.get('/api/v1/library/files', () =>
      HttpResponse.json({ items: FILES, meta: { total: 2, current_page: 1, per_page: 50, last_page: 1 } }),
    ),
    http.get('/api/v1/library/stats', () =>
      HttpResponse.json({ total_files: 2, total_folders: 0, total_size_bytes: 1, disk_free_bytes: 1, disk_total_bytes: 2 }),
    ),
    http.get('/api/v1/settings/', () => HttpResponse.json({ check_updates: false, check_printer_firmware: false })),
    http.get('/api/v1/settings/ui-preferences', () => HttpResponse.json({ use_slicer_api: useSlicerApi })),
    http.get('/api/v1/slicer/presets', () =>
      HttpResponse.json({
        local: {
          printer: [local('1', 'My X1C')],
          process: [local('10', '0.20mm Mine')],
          filament: [local('20', 'My PLA', { filament_type: 'PLA' })],
        },
        orca_cloud: EMPTY,
        cloud: EMPTY,
        standard: EMPTY,
        cloud_status: 'ok',
        orca_cloud_status: 'ok',
      }),
    ),
    http.get('/api/v1/slicer/printer-models', () => HttpResponse.json({})),
    http.get('/api/v1/library/assemble/printer-info', () =>
      HttpResponse.json({ model: 'X1C', bed_size: [256, 256], nozzles: [{ index: 1, area: [0, 0, 256, 256], max_height: 250 }] }),
    ),
    http.post('/api/v1/library/assemble', () =>
      HttpResponse.json({ job_id: 31, status: 'pending', status_url: '/api/v1/slice-jobs/31' }, { status: 202 }),
    ),
    http.get('/api/v1/slice-jobs/31', () =>
      HttpResponse.json({
        job_id: 31,
        status: 'completed',
        kind: 'assembly',
        progress: null,
        result: {
          outcome: 'ok',
          reason: '',
          parts: [],
          printers: [
            {
              label: 'My X1C',
              model: 'X1C',
              status: 'ok',
              reason: '',
              detail: '',
              parts: [],
              files: WRITTEN,
              plates: [],
              fill_counts: {},
              fill_limited: [],
            },
          ],
          trashed: [],
          removed: [],
          kept: [],
        },
      }),
    ),
    // What the Slice dialog reads about the file it is opened for.
    http.get('/api/v1/library/files/:id/plates', () => HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }] })),
    http.get('/api/v1/library/files/:id/filament-requirements', () =>
      HttpResponse.json({ file_id: 77, filename: 'x.3mf', filaments: [] }),
    ),
    http.get('/api/v1/library/files/:id/assembly', ({ params }) => {
      recordAsked.push(String(params.id));
      return HttpResponse.json({ detail: 'gone' }, { status: 404 });
    }),
    http.get('/api/v1/slicer/health/:kind', () => HttpResponse.json({ healthy: false, error: 'offline', url: null })),
    http.get('/api/v1/slicer/preset-values', () => HttpResponse.json({ resolved: false, values: {}, reason: 'x' })),
  );
}

/** The page with two models selected, taken through "Assemble project" to its summary. */
async function assembled(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  window.history.replaceState({}, '', '/files?selected=1,2');
  render(
    <SliceJobTrackerProvider>
      <FileManagerPage />
    </SliceJobTrackerProvider>,
  );
  await user.click(await screen.findByRole('button', { name: 'Assemble project' }));
  const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
  const assemble = within(dialog).getByRole('button', { name: 'Assemble' });
  await waitFor(() => expect(assemble).toBeEnabled());
  await user.click(assemble);
  return screen.findByTestId('assemble-summary');
}

describe('FileManagerPage — slicing a file "Assemble project" wrote', () => {
  beforeEach(() => {
    localStorage.clear();
    auth.granted = new Set(['library:read_all', 'library:update_all', 'library:upload']);
  });

  it('is not offered where the file rows offer no in-app slicing', async () => {
    serve(false);
    const summary = await assembled(userEvent.setup());

    expect(within(summary).getByText(WRITTEN[0].name)).toBeInTheDocument();
    expect(within(summary).queryByRole('button', { name: /^Slice / })).toBeNull();
  });

  it('opens the Slice dialog for that file over the summary, which keeps the other files', async () => {
    const user = userEvent.setup();
    serve(true);
    const summary = await assembled(user);

    await user.click(within(summary).getByRole('button', { name: `Slice ${WRITTEN[0].name}` }));

    const slice = await screen.findByRole('dialog', { name: 'Slice model' });
    expect(within(slice).getByText(WRITTEN[0].name)).toBeInTheDocument();
    // A file the assembly wrote: the dialog asks what it was built from.
    await waitFor(() => expect(recordAsked).toEqual(['77']));
    // The summary is still there, beneath: the second file can be sliced next.
    expect(screen.getByTestId('assemble-summary')).toBe(summary);
    expect(within(summary).getByRole('button', { name: `Slice ${WRITTEN[1].name}` })).toBeInTheDocument();
    expect(screen.getAllByRole('dialog', { hidden: true })).toHaveLength(2);
  });

  it('returns the focus to the button that opened the Slice dialog when it is closed', async () => {
    const user = userEvent.setup();
    serve(true);
    const summary = await assembled(user);
    const opener = within(summary).getByRole('button', { name: `Slice ${WRITTEN[0].name}` });

    await user.click(opener);
    const slice = await screen.findByRole('dialog', { name: 'Slice model' });
    await user.click(within(slice).getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Slice model' })).toBeNull());
    // Not `<body>`: a keyboard user is back where they were.
    await waitFor(() => expect(document.activeElement).toBe(opener));
    expect(screen.getByTestId('assemble-summary')).toBe(summary);
  });
});
