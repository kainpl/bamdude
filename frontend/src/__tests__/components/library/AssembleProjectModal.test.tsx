/**
 * "Assemble project": the dialog from its form to its summary, against the wire the backend speaks.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import type { AssemblyJobResult, LibraryFileListItem } from '../../../api/client';
import { AssembleProjectModal, type AssembleMode } from '../../../components/library/AssembleProjectModal';
import { SliceJobTrackerProvider } from '../../../contexts/SliceJobTrackerContext';
import { server } from '../../mocks/server';
import { render } from '../../utils';

const preset = (id: string, name: string, extra: Record<string, unknown> = {}) => ({ id, name, source: 'local', ...extra });
const EMPTY = { printer: [], process: [], filament: [] };
const PRESETS = {
  local: {
    printer: [preset('1', 'My X1C'), preset('2', 'My H2D')],
    process: [preset('10', '0.20mm Standard')],
    filament: [preset('20', 'My PLA', { filament_type: 'PLA' })],
  },
  orca_cloud: EMPTY,
  cloud: EMPTY,
  standard: EMPTY,
};
const INFO: Record<string, Record<string, unknown>> = {
  '1': { model: 'X1C', bed_size: [256, 256], nozzles: [{ index: 1, area: [0, 0, 256, 256], max_height: 250 }] },
  '2': {
    model: 'H2D',
    bed_size: [350, 320],
    nozzles: [
      { index: 1, area: [0, 0, 325, 320], max_height: 320 },
      { index: 2, area: [25, 0, 350, 320], max_height: 325 },
    ],
  },
};
const FILES = [
  { id: 4, filename: 'jar.stl', file_type: 'stl' },
  { id: 9, filename: 'lid.stl', file_type: 'stl' },
] as LibraryFileListItem[];

const PICTURE = 'data:image/png;base64,iVBORw0KGgo=';
const okResult = (over: Partial<AssemblyJobResult> = {}): AssemblyJobResult => ({
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
      files: [{ library_file_id: 77, name: 'jar +1 — X1C.3mf', plate: null, fallback_reason: null }],
      plates: [
        { plate: 0, picture: PICTURE, counts: { '0': 1 }, fill: false },
        { plate: 1, picture: PICTURE, counts: { '1': 14 }, fill: true },
      ],
      fill_counts: { '1': 14 },
      fill_limited: [],
    },
  ],
  trashed: [],
  removed: [],
  kept: [],
  ...over,
});

let posted: { url: string; body: Record<string, unknown> }[];
let jobState: Record<string, unknown>;
let cancelled: number;

function open(
  mode: AssembleMode = { kind: 'new', files: FILES },
  onOpenFile = vi.fn(),
  onSliceFile?: (fileId: number, name: string) => void,
) {
  const onClose = vi.fn();
  render(
    <SliceJobTrackerProvider>
      <AssembleProjectModal mode={mode} onClose={onClose} onOpenFile={onOpenFile} onSliceFile={onSliceFile} />
    </SliceJobTrackerProvider>,
  );
  return { onClose, onOpenFile };
}

const part = (index: number) => screen.getByTestId(`assemble-part-${index}`);
const target = (index: number) => screen.getByTestId(`assemble-target-${index}`);

beforeEach(() => {
  posted = [];
  cancelled = 0;
  jobState = { job_id: 31, status: 'completed', kind: 'assembly', progress: null, result: okResult() };
  const capture = async ({ request }: { request: Request }) => {
    posted.push({ url: new URL(request.url).pathname, body: (await request.json()) as Record<string, unknown> });
    return HttpResponse.json({ job_id: 31, status: 'pending', status_url: '/api/v1/slice-jobs/31' }, { status: 202 });
  };
  server.use(
    http.get('/api/v1/slicer/presets', () => HttpResponse.json(PRESETS)),
    http.get('/api/v1/slicer/printer-models', () => HttpResponse.json({})),
    http.get('/api/v1/library/assemble/printer-info', ({ request }) =>
      HttpResponse.json(INFO[new URL(request.url).searchParams.get('id') ?? '']),
    ),
    http.post('/api/v1/library/assemble', capture),
    http.post('/api/v1/library/files/:id/relayout', capture),
    http.get('/api/v1/slice-jobs/31', () => HttpResponse.json(jobState)),
    http.delete('/api/v1/slice-jobs/31', () => {
      cancelled += 1;
      return new HttpResponse(null, { status: 204 });
    }),
  );
});

describe('AssembleProjectModal', () => {
  it('shows a row per file and a first printer with presets already picked', async () => {
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await screen.findByTestId('assemble-part-0'); // the form waits for the preset listing
    expect(within(part(0)).getByText('jar.stl')).toBeInTheDocument();
    expect(within(part(1)).getByText('lid.stl')).toBeInTheDocument();
    await waitFor(() => expect(within(target(0)).getByRole('combobox', { name: 'Printer' })).toHaveValue('local:1'));
    expect(within(target(0)).getByRole('combobox', { name: 'Process' })).toHaveValue('local:10');
    expect(within(target(0)).getByRole('combobox', { name: 'Filament' })).toHaveValue('local:20');
    expect(within(dialog).getByRole('textbox', { name: 'Name of the result' })).toHaveValue('jar +1');
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
  });

  it('lets "Fill a plate" stand in for the number of copies', async () => {
    open();
    await screen.findByTestId('assemble-part-0');
    const quantity = within(part(1)).getByRole('spinbutton', { name: 'Copies of lid.stl' });
    fireEvent.click(within(part(1)).getByRole('button', { name: 'More copies of lid.stl' }));
    expect(quantity).toHaveValue(2);
    fireEvent.click(within(part(1)).getByRole('checkbox', { name: 'Fill a plate with lid.stl' }));
    expect(quantity).toBeDisabled();
    expect(within(part(1)).getByRole('button', { name: 'More copies of lid.stl' })).toBeDisabled();
  });

  it('adds and removes printers, and offers the nozzle only where there are two', async () => {
    open();
    await waitFor(() => expect(within(target(0)).getByRole('combobox', { name: 'Printer' })).toHaveValue('local:1'));
    expect(within(target(0)).queryByRole('combobox', { name: 'Nozzle of printer 1' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove printer 1' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Add printer' }));
    fireEvent.change(within(target(1)).getByRole('combobox', { name: 'Printer' }), { target: { value: 'local:2' } });
    const nozzle = await within(target(1)).findByRole('combobox', { name: 'Nozzle of printer 2' });
    expect(within(nozzle).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'Left nozzle',
      'Right nozzle',
    ]);
    fireEvent.click(screen.getByRole('button', { name: 'Remove printer 1' }));
    expect(screen.queryByTestId('assemble-target-1')).not.toBeInTheDocument();
    expect(within(target(0)).getByRole('combobox', { name: 'Printer' })).toHaveValue('local:2');
  });

  it('posts the form and shows the plates of the finished job', async () => {
    const { onOpenFile } = open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(part(1)).getByRole('checkbox', { name: 'Fill a plate with lid.stl' }));
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Gap between parts, mm' }), { target: { value: '3' } });
    fireEvent.click(within(dialog).getByRole('switch', { name: 'A separate file for each plate' }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));

    const summary = await screen.findByTestId('assemble-summary');
    expect(posted).toHaveLength(1);
    expect(posted[0].url).toBe('/api/v1/library/assemble');
    expect(posted[0].body).toEqual({
      parts: [
        { file_id: 4, quantity: 1, fill: false, orientation: 'as_is' },
        { file_id: 9, quantity: 1, fill: true, orientation: 'as_is' },
      ],
      targets: [
        {
          printer_preset: { source: 'local', id: '1' },
          process_preset: { source: 'local', id: '10' },
          filament_preset: { source: 'local', id: '20' },
          nozzle: 1,
        },
      ],
      layout: { spacing: 3, rotation_step: 90, centre: true, split_plates: true },
      name: 'jar +1',
    });
    expect(within(summary).getByText('The project is assembled.')).toBeInTheDocument();
    expect(within(summary).getAllByRole('img')).toHaveLength(2);
    expect(within(summary).getByText('lid.stl — 14 per plate')).toBeInTheDocument();
    fireEvent.click(within(summary).getByRole('button', { name: 'jar +1 — X1C.3mf' }));
    expect(onOpenFile).toHaveBeenCalledWith(77);
  });

  it('offers to slice each file it wrote, and hands over that file', async () => {
    const onSliceFile = vi.fn();
    const { onOpenFile } = open(undefined, vi.fn(), onSliceFile);
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));

    const summary = await screen.findByTestId('assemble-summary');
    const files = okResult().printers.flatMap((row) => row.files);
    expect(within(summary).getAllByRole('button', { name: /^Slice / })).toHaveLength(files.length);
    fireEvent.click(within(summary).getByRole('button', { name: 'Slice jar +1 — X1C.3mf' }));

    expect(onSliceFile).toHaveBeenCalledTimes(1);
    expect(onSliceFile).toHaveBeenCalledWith(77, 'jar +1 — X1C.3mf');
    // slicing a file is not opening it
    expect(onOpenFile).not.toHaveBeenCalled();
  });

  it('offers no slicing where the page gave it no way to slice', async () => {
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));

    const summary = await screen.findByTestId('assemble-summary');
    expect(within(summary).queryByRole('button', { name: /^Slice / })).not.toBeInTheDocument();
  });

  it('names the parts a printer could not take, beside the printer that could', async () => {
    const result = okResult();
    result.printers.push({
      label: 'My A1 mini',
      model: 'A1MINI',
      status: 'failed',
      reason: 'part_rejected',
      detail: '',
      parts: [{ index: 0, name: 'jar.stl', reason: 'too_large' }],
      files: [],
      plates: [],
      fill_counts: {},
      fill_limited: [],
    });
    jobState = { ...jobState, result };
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));
    const rows = await screen.findAllByTestId('assemble-summary-row');
    expect(rows).toHaveLength(2);
    expect(within(rows[1]).getByText('Some parts do not fit this printer')).toBeInTheDocument();
    expect(within(rows[1]).getByText('jar.stl — larger than the print area')).toBeInTheDocument();
    expect(screen.getByText(/assembled, but not for every printer/)).toBeInTheDocument();
    expect(screen.queryByText('The project is assembled.')).not.toBeInTheDocument();
  });

  it('says why a job that saved nothing failed', async () => {
    jobState = { ...jobState, result: okResult({ outcome: 'failed', reason: 'source_changed', printers: [] }) };
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));
    const summary = await screen.findByTestId('assemble-summary');
    expect(within(summary).getByText(/A source file changed while the project was being assembled/)).toBeInTheDocument();
  });

  it('can stop a running job, and closing does not stop it', async () => {
    jobState = {
      job_id: 31,
      status: 'running',
      kind: 'assembly',
      progress: { stage: 'packing', printer_index: 0, printer_count: 2, placed: 5, total: 9 },
    };
    const { onClose } = open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));
    expect(await screen.findByText('Printer 1 of 2 — placed 5 of 9')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    await waitFor(() => expect(cancelled).toBe(1));
    fireEvent.click(within(dialog).getAllByRole('button', { name: 'Close' })[0]);
    expect(onClose).toHaveBeenCalled();
    expect(cancelled).toBe(1);
  });

  it('does not start a re-layout with a preset that is no longer there', async () => {
    server.use(
      http.get('/api/v1/library/files/57/assembly', () =>
        HttpResponse.json({
          set_id: 'b'.repeat(32),
          base_name: 'jars',
          parts: [{ name: 'jar.stl', quantity: 1, fill: false, orientation: 'as_is' }],
          printer: {
            printer: { source: 'local', id: '1' },
            process: { source: 'local', id: '99' },
            filament: { source: 'local', id: '20' },
            nozzle: 1,
            model: 'X1C',
          },
          layout: { spacing: 2, rotation_step: 90, centre: true, split_plates: false },
          plates: [],
          set_files: [{ id: 57, filename: 'jars.3mf' }],
        }),
      ),
    );
    open({ kind: 'relayout', fileId: 57, filename: 'jars.3mf' });
    const dialog = await screen.findByRole('dialog', { name: 'Re-layout project' });
    expect(
      await within(dialog).findByText('A chosen preset is no longer available — choose another one'),
    ).toBeInTheDocument();
    const start = within(dialog).getByRole('button', { name: 'Re-layout' });
    expect(start).toBeDisabled();
    fireEvent.change(within(target(0)).getByRole('combobox', { name: 'Process' }), { target: { value: 'local:10' } });
    await waitFor(() => expect(start).toBeEnabled());
    expect(within(dialog).queryByText(/no longer available/)).not.toBeInTheDocument();
  });

  it('says that a previous file on an external share was removed, not trashed', async () => {
    jobState = { job_id: 31, status: 'completed', kind: 'assembly', result: okResult({ removed: [5] }) };
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));
    const summary = await screen.findByTestId('assemble-summary');
    expect(
      within(summary).getByText('1 previous file was removed from the library; the file itself stays on its share.'),
    ).toBeInTheDocument();
  });

  it('says which STEP file is being converted before anything is laid out', async () => {
    jobState = {
      job_id: 31,
      status: 'running',
      kind: 'assembly',
      progress: { stage: 'converting', convert: 2, convert_count: 3 },
    };
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));
    expect(await screen.findByText('Converting STEP file 2 of 3…')).toBeInTheDocument();
  });

  it('counts what is placed while a plate is being filled, without a made-up total', async () => {
    jobState = {
      job_id: 31,
      status: 'running',
      kind: 'assembly',
      progress: { stage: 'packing', printer_index: 1, printer_count: 2, placed: 41, total: 1000, fill_part: 1 },
    };
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));
    expect(await screen.findByText('Printer 2 of 2 — filling a plate, 41 placed')).toBeInTheDocument();
    expect(screen.queryByText(/1000/)).not.toBeInTheDocument();
  });

  it('says it is laying out before the first count arrives', async () => {
    jobState = { job_id: 31, status: 'running', kind: 'assembly', progress: { stage: 'packing' } };
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));
    expect(await screen.findByText('Laying out…')).toBeInTheDocument();
  });

  it('says so when the state of the job can no longer be read', async () => {
    server.use(http.get('/api/v1/slice-jobs/31', () => HttpResponse.json({ detail: 'Slice job not found or expired' }, { status: 404 })));
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));
    expect(await screen.findByText(/The state of the assembly can no longer be read/)).toBeInTheDocument();
  });

  it('does not offer to stop a job that is already saving its files', async () => {
    jobState = { job_id: 31, status: 'running', kind: 'assembly', progress: { stage: 'publishing' } };
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));
    expect(await screen.findByText('Saving to the library…')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeDisabled();
  });

  it('shows the server’s refusal beside the button and stays on the form', async () => {
    server.use(
      http.post('/api/v1/library/assemble', () =>
        HttpResponse.json({ detail: 'STEP files cannot be assembled yet: drawing.step' }, { status: 400 }),
      ),
    );
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Assemble' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('STEP files cannot be assembled yet: drawing.step');
    expect(screen.getByTestId('assemble-part-0')).toBeInTheDocument();
  });

  it('opens a re-layout with the stored values and posts what to do with the previous file', async () => {
    server.use(
      http.get('/api/v1/library/files/55/assembly', () =>
        HttpResponse.json({
          set_id: 'a'.repeat(32),
          base_name: 'jars',
          parts: [
            { name: 'jar.stl', quantity: 6, fill: false, orientation: 'as_is' },
            { name: 'lid.stl', quantity: 1, fill: true, orientation: 'as_is' },
          ],
          printer: {
            printer: { source: 'local', id: '2' },
            process: { source: 'local', id: '10' },
            filament: { source: 'local', id: '20' },
            nozzle: 2,
            model: 'H2D',
          },
          layout: { spacing: 4, rotation_step: 45, centre: false, split_plates: false },
          plates: [],
          set_files: [{ id: 55, filename: 'jars — H2D.3mf' }],
        }),
      ),
    );
    open({ kind: 'relayout', fileId: 55, filename: 'jars — H2D.3mf' });
    const dialog = await screen.findByRole('dialog', { name: 'Re-layout project' });
    expect(await within(dialog).findByRole('spinbutton', { name: 'Copies of jar.stl' })).toHaveValue(6);
    expect(within(part(1)).getByRole('checkbox', { name: 'Fill a plate with lid.stl' })).toBeChecked();
    expect(within(target(0)).getByRole('combobox', { name: 'Printer' })).toHaveValue('local:2');
    await waitFor(() => expect(within(target(0)).getByRole('combobox', { name: 'Nozzle of printer 1' })).toHaveValue('2'));
    expect(within(dialog).getByRole('textbox', { name: 'Gap between parts, mm' })).toHaveValue('4');
    const trash = within(dialog).getByRole('checkbox', { name: 'Move the previous file to the trash' });
    expect(trash).toBeChecked();
    fireEvent.click(trash);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Re-layout' }));
    await screen.findByTestId('assemble-summary');
    expect(posted[0].url).toBe('/api/v1/library/files/55/relayout');
    expect(posted[0].body).toMatchObject({
      trash_previous: false,
      name: 'jars',
      parts: [
        { quantity: 6, fill: false, orientation: 'as_is' },
        { quantity: 1, fill: true, orientation: 'as_is' },
      ],
      layout: { spacing: 4, rotation_step: 45, centre: false, split_plates: false },
    });
    expect((posted[0].body.targets as { nozzle: number }[])[0].nozzle).toBe(2);
  });

  it('starts on a machine preset, not on a model card that has no print area', async () => {
    server.use(
      http.get('/api/v1/slicer/presets', () =>
        HttpResponse.json({
          ...PRESETS,
          local: EMPTY,
          standard: {
            printer: [
              { id: 'Bambu Lab A1', name: 'Bambu Lab A1', source: 'standard' },
              { id: 'Bambu Lab A1 0.4 nozzle', name: 'Bambu Lab A1 0.4 nozzle', source: 'standard' },
            ],
            process: [{ id: 'p', name: '0.20mm Standard @BBL A1', source: 'standard' }],
            filament: [{ id: 'f', name: 'Bambu PLA Basic @BBL A1', source: 'standard', filament_type: 'PLA' }],
          },
        }),
      ),
      http.get('/api/v1/library/assemble/printer-info', ({ request }) =>
        new URL(request.url).searchParams.get('id') === 'Bambu Lab A1'
          ? HttpResponse.json(
              { detail: { error: 'geometry_unknown', message: 'The printer preset does not describe its print area' } },
              { status: 422 },
            )
          : HttpResponse.json(INFO['1']),
      ),
    );
    open();
    const dialog = await screen.findByRole('dialog', { name: 'Assemble project' });
    const printer = await within(dialog).findByRole('combobox', { name: 'Printer' });
    await waitFor(() => expect(printer).toHaveValue('standard:Bambu Lab A1 0.4 nozzle'));
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeEnabled());

    fireEvent.change(printer, { target: { value: 'standard:Bambu Lab A1' } });
    expect(await within(dialog).findByText('The printer preset does not describe its print area')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Assemble' })).toBeDisabled();
  });

  it('says so when the file carries no record', async () => {
    server.use(
      http.get('/api/v1/library/files/56/assembly', () =>
        HttpResponse.json(
          { detail: { error: 'not_assembled', message: 'This file was not assembled here, or its record is damaged' } },
          { status: 409 },
        ),
      ),
    );
    open({ kind: 'relayout', fileId: 56, filename: 'other.3mf' });
    expect(await screen.findByText('This file was not assembled here, or its record is damaged')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Re-layout' })).not.toBeInTheDocument();
  });
});
