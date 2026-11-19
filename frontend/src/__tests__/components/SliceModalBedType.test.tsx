/**
 * The Slice dialog's bed plate.
 *
 * One list, the slice request's own. `Textured Cool Plate` is in OrcaSlicer's
 * `curr_bed_type` enum and not in Bambu Studio's, so it is offered only while
 * OrcaSlicer is the slicer — and a bed plate the dialog does not apply (from
 * saved settings, or one the chosen slicer lacks) is SAID, never dropped in
 * silence.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { SliceModal } from '../../components/SliceModal';
import { SliceJobTrackerProvider } from '../../contexts/SliceJobTrackerContext';

const BED_KEY = 'bamdude:slice-modal:bed-type';
const X1C = 'Bambu Lab X1 Carbon 0.4 nozzle';

function serve({
  healthy,
  pipelines = [],
}: {
  healthy: ('orcaslicer' | 'bambu_studio')[];
  pipelines?: { id: number; name: string; bed_type: string | null }[];
}) {
  const ref = (id: string) => ({ source: 'local', id });
  server.use(
    http.get('*/api/v1/library/files/:id/plates', () =>
      HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }] }),
    ),
    http.get('*/api/v1/library/files/:id/filament-requirements', () =>
      HttpResponse.json({ file_id: 1, filename: 'Cube.3mf', filaments: [] }),
    ),
    http.get('*/api/v1/slicer/presets', () =>
      HttpResponse.json({
        local: {
          printer: [{ id: 'p', name: X1C, source: 'local' }],
          process: [{ id: 'q', name: '0.20mm Mine', source: 'local' }],
          filament: [{ id: 'f', name: 'My PLA', source: 'local', filament_type: 'PLA' }],
        },
        orca_cloud: { printer: [], process: [], filament: [] },
        cloud: { printer: [], process: [], filament: [] },
        standard: { printer: [], process: [], filament: [] },
        cloud_status: 'ok',
        orca_cloud_status: 'ok',
      }),
    ),
    http.get('*/api/v1/slicer/printer-models', () => HttpResponse.json({ 'Bambu Lab X1 Carbon': 'X1C' })),
    http.get('*/api/v1/slicer/health/:kind', ({ params }) =>
      healthy.includes(params.kind as 'orcaslicer' | 'bambu_studio')
        ? HttpResponse.json({ healthy: true, version: '2.5.0', url: 'http://sidecar' })
        : HttpResponse.json({ healthy: false, error: 'offline', url: null }),
    ),
    http.get('*/api/v1/slicer/preset-values', () => HttpResponse.json({ resolved: false, values: {}, reason: 'x' })),
    http.get('*/api/v1/slicer-pipelines/', () =>
      HttpResponse.json({
        pipelines: pipelines.map((p) => ({
          ...p,
          description: null,
          printer_preset: ref('p'),
          process_preset: ref('q'),
          filament_presets: [ref('f')],
          created_by: null,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        })),
      }),
    ),
  );
}

function open() {
  localStorage.setItem('bamdude:presets:only-farm', 'off');
  localStorage.setItem('bamdude:presets:only-mine', 'off');
  return render(
    <SliceJobTrackerProvider>
      <SliceModal source={{ kind: 'libraryFile', id: 1, filename: 'Cube.3mf' }} onClose={() => undefined} />
    </SliceJobTrackerProvider>,
  );
}

const bedPicker = async () =>
  (await waitFor(() => screen.getByRole('combobox', { name: 'Bed plate' }))) as HTMLSelectElement;
const bedOptions = (select: HTMLSelectElement) => within(select).getAllByRole('option').map((o) => o.textContent);

afterEach(() => localStorage.clear());

describe('SliceModal — Textured Cool Plate', () => {
  it('is offered while OrcaSlicer is the slicer', async () => {
    serve({ healthy: ['orcaslicer'] });
    open();

    const bed = await bedPicker();
    await waitFor(() => expect(bedOptions(bed)).toContain('Textured Cool Plate'));
  });

  it('is not offered for Bambu Studio', async () => {
    serve({ healthy: ['bambu_studio'] });
    open();

    const bed = await bedPicker();
    await waitFor(() => expect(screen.getByText(/Ready/)).toBeInTheDocument());
    expect(bedOptions(bed)).toEqual([
      'Cool Plate',
      'Engineering Plate',
      'Smooth PEI / High Temp Plate',
      'Textured PEI Plate',
      'Cool Plate SuperTack',
    ]);
  });

  it('is taken back, and said so, when the slicer turns out to be Bambu Studio', async () => {
    localStorage.setItem(BED_KEY, 'Textured Cool Plate');
    serve({ healthy: ['bambu_studio'] });
    open();

    const bed = await bedPicker();
    await waitFor(() => expect(bed.value).toBe('Textured PEI Plate'));
    expect(screen.getByRole('status', { name: 'Bed plate' })).toHaveTextContent(
      /Textured Cool Plate.*only.*OrcaSlicer.*Textured PEI Plate/,
    );
  });

  it('stays the pick, with nothing to say, when OrcaSlicer is the slicer', async () => {
    localStorage.setItem(BED_KEY, 'Textured Cool Plate');
    serve({ healthy: ['orcaslicer'] });
    open();

    const bed = await bedPicker();
    await waitFor(() => expect(bedOptions(bed)).toContain('Textured Cool Plate'));
    expect(bed.value).toBe('Textured Cool Plate');
    expect(screen.queryByRole('status', { name: 'Bed plate' })).toBeNull();
  });
});

describe('SliceModal — the bed plate of saved settings', () => {
  const apply = async (name: string) => {
    const user = userEvent.setup();
    const pipelines = (await waitFor(() => screen.getByLabelText('Saved settings'))) as HTMLSelectElement;
    await waitFor(() => expect(within(pipelines).getByRole('option', { name })).toBeInTheDocument());
    await user.selectOptions(pipelines, name);
    return user;
  };

  it('says when the saved bed plate is one nobody knows, and leaves the pick alone', async () => {
    serve({ healthy: ['orcaslicer'], pipelines: [{ id: 1, name: 'Old set', bed_type: 'Smooth PEI Plate' }] });
    open();

    const bed = await bedPicker();
    await apply('Old set');

    expect(await screen.findByRole('status', { name: 'Bed plate' })).toHaveTextContent(/Smooth PEI Plate/);
    expect(bed.value).toBe('Textured PEI Plate');
  });

  it('says when the saved bed plate is one the chosen slicer lacks', async () => {
    serve({ healthy: ['bambu_studio'], pipelines: [{ id: 2, name: 'Orca set', bed_type: 'Textured Cool Plate' }] });
    open();

    const bed = await bedPicker();
    await waitFor(() => expect(screen.getByText(/Ready/)).toBeInTheDocument());
    await apply('Orca set');

    expect(await screen.findByRole('status', { name: 'Bed plate' })).toHaveTextContent(
      /Textured Cool Plate.*only.*OrcaSlicer/,
    );
    expect(bed.value).toBe('Textured PEI Plate');
  });

  it('applies a bed plate it knows without a word', async () => {
    serve({ healthy: ['orcaslicer'], pipelines: [{ id: 3, name: 'Fine set', bed_type: 'Textured Cool Plate' }] });
    open();

    const bed = await bedPicker();
    await waitFor(() => expect(bedOptions(bed)).toContain('Textured Cool Plate'));
    await apply('Fine set');

    await waitFor(() => expect(bed.value).toBe('Textured Cool Plate'));
    expect(screen.queryByRole('status', { name: 'Bed plate' })).toBeNull();
  });

  it('takes the note away once the bed plate is picked by hand', async () => {
    serve({ healthy: ['orcaslicer'], pipelines: [{ id: 1, name: 'Old set', bed_type: 'Smooth PEI Plate' }] });
    open();

    const bed = await bedPicker();
    const user = await apply('Old set');
    await screen.findByRole('status', { name: 'Bed plate' });
    await user.selectOptions(bed, 'Cool Plate');

    expect(screen.queryByRole('status', { name: 'Bed plate' })).toBeNull();
  });
});

describe('SliceModal — the remembered bed plate', () => {
  it('is not overwritten by the switch the dialog makes for Bambu Studio', async () => {
    localStorage.setItem(BED_KEY, 'Textured Cool Plate');
    serve({ healthy: ['bambu_studio'] });
    open();

    const bed = await bedPicker();
    await waitFor(() => expect(bed.value).toBe('Textured PEI Plate'));
    await screen.findByRole('status', { name: 'Bed plate' });
    // The next OrcaSlicer dialog starts on the plate the user chose.
    expect(localStorage.getItem(BED_KEY)).toBe('Textured Cool Plate');
  });

  it('is what the user picks by hand', async () => {
    const user = userEvent.setup();
    serve({ healthy: ['orcaslicer'] });
    open();

    const bed = await bedPicker();
    await user.selectOptions(bed, 'Cool Plate');

    expect(localStorage.getItem(BED_KEY)).toBe('Cool Plate');
  });
});

describe('SliceModal — the bed plate note after the state it describes is gone', () => {
  const both: ('orcaslicer' | 'bambu_studio')[] = ['orcaslicer', 'bambu_studio'];
  const slicerCard = (name: RegExp) => screen.findByRole('button', { name });
  const apply = async (user: ReturnType<typeof userEvent.setup>, name: string) => {
    const pipelines = (await waitFor(() => screen.getByLabelText('Saved settings'))) as HTMLSelectElement;
    await waitFor(() => expect(within(pipelines).getByRole('option', { name })).toBeInTheDocument());
    await user.selectOptions(pipelines, name);
  };

  it('puts the plate back and drops the note when OrcaSlicer becomes the slicer', async () => {
    const user = userEvent.setup();
    localStorage.setItem(BED_KEY, 'Textured Cool Plate');
    serve({ healthy: both });
    open();

    const bed = await bedPicker();
    await user.click(await slicerCard(/BambuStudio/));
    await waitFor(() => expect(bed.value).toBe('Textured PEI Plate'));
    await screen.findByRole('status', { name: 'Bed plate' });

    await user.click(await slicerCard(/OrcaSlicer/));

    await waitFor(() => expect(screen.queryByRole('status', { name: 'Bed plate' })).toBeNull());
    expect(bed.value).toBe('Textured Cool Plate');
  });

  it('drops "available only with OrcaSlicer" when OrcaSlicer becomes the slicer', async () => {
    const user = userEvent.setup();
    serve({ healthy: both, pipelines: [{ id: 2, name: 'Orca set', bed_type: 'Textured Cool Plate' }] });
    open();

    await bedPicker();
    await user.click(await slicerCard(/BambuStudio/));
    await apply(user, 'Orca set');
    await screen.findByRole('status', { name: 'Bed plate' });

    await user.click(await slicerCard(/OrcaSlicer/));

    await waitFor(() => expect(screen.queryByRole('status', { name: 'Bed plate' })).toBeNull());
  });

  it('drops the note of earlier saved settings when later ones name no plate', async () => {
    const user = userEvent.setup();
    serve({
      healthy: ['orcaslicer'],
      pipelines: [
        { id: 1, name: 'Old set', bed_type: 'Smooth PEI Plate' },
        { id: 2, name: 'Plain set', bed_type: null },
      ],
    });
    open();

    await bedPicker();
    await apply(user, 'Old set');
    await screen.findByRole('status', { name: 'Bed plate' });
    await apply(user, 'Plain set');

    await waitFor(() => expect(screen.queryByRole('status', { name: 'Bed plate' })).toBeNull());
  });
});
