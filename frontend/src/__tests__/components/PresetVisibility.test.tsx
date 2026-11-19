import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { SliceModal } from '../../components/SliceModal';
import { AssembleProjectModal } from '../../components/library/AssembleProjectModal';
import { SliceJobTrackerProvider } from '../../contexts/SliceJobTrackerContext';
import type { LibraryFileListItem } from '../../api/client';

const MINI = 'Bambu Lab A1 mini 0.4 nozzle';
const A1 = 'Bambu Lab A1 0.4 nozzle';
const preset = (id: string, name: string, source = 'local', extra = {}) => ({ id, name, source, ...extra });
const listing = {
  local: { printer: [], process: [preset('10', 'My 3-3-3-15', 'local', { compatible_printers: [MINI] })], filament: [preset('20', '333 Print', 'local', { filament_type: 'PETG', compatible_printers: [MINI] })] },
  orca_cloud: { printer: [], process: [], filament: [] },
  cloud: { printer: [], process: [], filament: [] },
  standard: { printer: [preset(A1, A1, 'standard'), preset('Bambu Lab A1 mini 0.2 nozzle', 'Bambu Lab A1 mini 0.2 nozzle', 'standard'), preset(MINI, MINI, 'standard')], process: [preset('stock-process', 'Stock process', 'standard')], filament: [preset('stock-filament', 'Stock PLA', 'standard', { filament_type: 'PLA' })] },
  cloud_status: 'not_authenticated', orca_cloud_status: 'not_authenticated',
};

beforeEach(() => {
  localStorage.removeItem('bamdude:presets:only-farm');
  localStorage.removeItem('bamdude:presets:only-mine');
  server.use(
    http.get('*/api/v1/printers/', () => HttpResponse.json([{ id: 1, name: 'Paused mini', model: 'A1Mini', archived: false, is_active: false, queue_paused: true }])),
    http.get('*/api/v1/slicer/presets', () => HttpResponse.json(listing)),
    http.get('*/api/v1/slicer/printer-models', () => HttpResponse.json({ 'Bambu Lab A1 mini': 'A1M', 'Bambu Lab A1': 'A1' })),
    http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }] })),
    http.get('*/api/v1/library/files/:id/filament-requirements', () => HttpResponse.json({ file_id: 1, filename: 'Cube.stl', filaments: [{ slot_id: 1, type: 'PETG', color: '', used_grams: 1, used_meters: 1 }] })),
    http.get('*/api/v1/slicer/health/:kind', () => HttpResponse.json({ healthy: true })),
    http.get('*/api/v1/slicer/preset-values', () => HttpResponse.json({ resolved: false, values: {}, reason: 'offline' })),
    http.get('*/api/v1/slicer/pipelines', () => HttpResponse.json({ pipelines: [] })),
    http.get('*/api/v1/library/assemble/printer-info', () => HttpResponse.json({ model: 'A1Mini', bed_size: [180, 180], nozzles: [{ index: 1, area: [0, 0, 180, 180], max_height: 180 }] })),
  );
});

describe.each(['slice', 'assemble'] as const)('%s preset visibility', (mode) => {
  const open = () => render(<SliceJobTrackerProvider>{mode === 'slice'
    ? <SliceModal source={{ kind: 'libraryFile', id: 1, filename: 'Cube.stl' }} onClose={() => undefined} />
    : <AssembleProjectModal mode={{ kind: 'new', files: [{ id: 1, filename: 'Cube.stl', file_type: 'stl' } as LibraryFileListItem] }} onClose={() => undefined} />
  }</SliceJobTrackerProvider>);
  const picker = (slot: 'Printer' | 'Process' | 'Filament') => screen.getByRole('combobox', { name: mode === 'slice' ? `${slot} profile` : slot });

  it('defaults to farm models and user profiles, including a paused, offline or maintenance model', async () => {
    open();
    await waitFor(() => expect(picker('Printer')).toHaveValue(`standard:${MINI}`));
    expect(screen.getByRole('checkbox', { name: 'Only models in my farm' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Only my processes and filaments' })).toBeChecked();
    expect(within(picker('Printer')).queryByRole('option', { name: A1 })).not.toBeInTheDocument();
    expect(within(picker('Filament')).getByRole('option', { name: '333 Print' })).toBeInTheDocument();
    expect(within(picker('Filament')).queryByRole('option', { name: 'Stock PLA' })).not.toBeInTheDocument();
    expect(picker('Process')).toHaveValue('local:10');
  });

  it('can reveal the catalogue and keeps an explicit selection when filters narrow it again', async () => {
    open();
    await waitFor(() => expect(picker('Printer')).toHaveValue(`standard:${MINI}`));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Only models in my farm' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Only my processes and filaments' }));
    expect(within(picker('Printer')).getByRole('option', { name: A1 })).toBeInTheDocument();
    fireEvent.change(picker('Filament'), { target: { value: 'standard:stock-filament' } });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Only my processes and filaments' }));
    expect(picker('Filament')).toHaveValue('standard:stock-filament');
    expect(within(picker('Filament')).getByRole('option', { name: 'Stock PLA' })).toBeInTheDocument();
  });

  it('does not auto-pick a custom process or filament made for another model', async () => {
    open();
    await waitFor(() => expect(picker('Printer')).toHaveValue(`standard:${MINI}`));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Only models in my farm' }));
    fireEvent.change(picker('Printer'), { target: { value: `standard:${A1}` } });
    await waitFor(() => expect(picker('Process')).toHaveValue(''));
    expect(picker('Filament')).toHaveValue('');
  });

  it('shows the catalogue with an explanation when the farm read fails', async () => {
    server.use(http.get('*/api/v1/printers/', () => new HttpResponse(null, { status: 503 })));
    open();
    expect(await screen.findByText('Could not read the farm models. All printers are shown.')).toBeInTheDocument();
    expect(within(picker('Printer')).getByRole('option', { name: A1 })).toBeInTheDocument();
    expect(within(picker('Printer')).getByRole('option', { name: MINI })).toBeInTheDocument();
  });
});
