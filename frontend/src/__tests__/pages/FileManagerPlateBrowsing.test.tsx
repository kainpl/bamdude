/**
 * The library card pages through a multi-plate file one plate at a time, and a
 * list row opens that very card in a dialog
 * (vault 60-specs/library-multiplate-card-spec 5, 6): the counter, the picture,
 * the figures and the materials all describe the CURRENT plate; a single-plate
 * file shows its materials and no arrows, and its row opens the same way.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';

const slice = (index: number, over: Record<string, unknown> = {}) => ({
  index,
  name: null,
  print_time_seconds: index * 3600,
  filament_used_grams: index * 10,
  object_count: index,
  filament_types: index === 2 ? ['PETG', 'PLA'] : ['PLA'],
  has_thumbnail: true,
  ...over,
});

const base = (id: number, name: string) => ({
  id,
  filename: `${name}.gcode.3mf`,
  file_path: `/library/${name}.gcode.3mf`,
  file_size: 1024,
  file_type: 'gcode',
  file_tags: ['gcode', '3mf', 'sliced'],
  product_ids: [],
  folder_id: null,
  thumbnail_path: 'thumb.png',
  print_name: name,
  print_time_seconds: 3600,
  filament_used_grams: 10,
  object_count: 1,
  filament_types: ['PLA'],
  duplicate_count: 0,
  print_count: 0,
  notes_count: 0,
  skip_objects_supported: false,
  swap_compatible: false,
  created_at: '2024-01-01T00:00:00Z',
});

const multi = {
  ...base(1, 'Multi'),
  file_tags: ['gcode', '3mf', 'sliced', 'multiplate'],
  is_multi_plate: true,
  plate_summaries: [slice(1), slice(2), slice(3, { has_thumbnail: false })],
};
const single = { ...base(2, 'Single'), filament_types: ['ASA', 'PETG'] };

function mockLibrary() {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
    http.get('/api/v1/library/files', () =>
      HttpResponse.json({ items: [multi, single], meta: { current_page: 1, per_page: 50, total: 2, last_page: 1 } }),
    ),
    http.get('/api/v1/library/stats', () =>
      HttpResponse.json({ total_files: 2, total_size: 2048, sliced_files: 2, unsliced_files: 0 }),
    ),
    http.get('/api/v1/settings/', () =>
      HttpResponse.json({ check_updates: false, check_printer_firmware: false, library_disk_warning_gb: 5 }),
    ),
    http.get('/api/v1/projects/', () => HttpResponse.json([])),
    http.get('/api/v1/library/tags', () => HttpResponse.json([])),
  );
}

const SETTLE = { timeout: 5000 };

/** The card (grid) or row (list) that carries this file's name.
 *
 * The name button's accessible name is its TEXT (the print name); the
 * «View prints of …» wording is only its title, which an accessible-name
 * query never reaches while the button has text content.
 */
function surfaceOf(name: string) {
  const heading = screen.getByText(name, { selector: 'button' });
  return heading.closest('[data-file-card], [data-file-row]') as HTMLElement;
}

describe('FileManagerPage - paging through plates', () => {
  beforeEach(() => {
    localStorage.clear();
    mockLibrary();
  });

  it('the grid card shows plate 1, then plate 2 on next, and wraps', async () => {
    render(<FileManagerPage />);
    const card = await waitFor(() => surfaceOf('Multi'), SETTLE);
    expect(within(card).getByTestId('plate-counter')).toHaveTextContent('1/3');
    expect(within(card).getByTestId('plate-materials')).toHaveTextContent('PLA');
    expect(within(card).getByText('10.0g')).toBeInTheDocument();
    expect(within(card).getByRole('img')).toHaveAttribute(
      'src',
      expect.stringContaining('/library/files/1/thumbnail'),
    );

    await userEvent.click(within(card).getByRole('button', { name: 'Next plate' }));
    expect(within(card).getByTestId('plate-counter')).toHaveTextContent('2/3');
    expect(within(card).getByTestId('plate-materials')).toHaveTextContent('PETG+PLA');
    expect(within(card).getByText('20.0g')).toBeInTheDocument();
    expect(within(card).getByRole('img')).toHaveAttribute('src', '/api/v1/library/files/1/plate-thumbnail/2');

    await userEvent.click(within(card).getByRole('button', { name: 'Next plate' }));
    expect(within(card).getByTestId('plate-counter')).toHaveTextContent('3/3');
    expect(within(card).queryByRole('img')).not.toBeInTheDocument(); // plate 3 has no picture

    await userEvent.click(within(card).getByRole('button', { name: 'Next plate' }));
    expect(within(card).getByTestId('plate-counter')).toHaveTextContent('1/3');
  });

  it('a single-plate card shows its materials and no arrows', async () => {
    render(<FileManagerPage />);
    const card = await waitFor(() => surfaceOf('Single'), SETTLE);
    expect(within(card).getByTestId('plate-materials')).toHaveTextContent('ASA+PETG');
    expect(within(card).queryByRole('button', { name: 'Next plate' })).not.toBeInTheDocument();
    expect(within(card).queryByTestId('plate-counter')).not.toBeInTheDocument();
  });

  it('the list row opens the very same card in a dialog - from the thumbnail and from the plates chip', async () => {
    localStorage.setItem('library-view-mode', 'list');
    render(<FileManagerPage />);
    const row = await waitFor(() => surfaceOf('Multi'), SETTLE);
    expect(within(row).getByTestId('plates-chip')).toHaveTextContent('3 plates');
    expect(within(row).getByTestId('plate-materials')).toHaveTextContent('PLA'); // plate 1's
    expect(within(row).queryByRole('button', { name: 'Next plate' })).not.toBeInTheDocument();

    await userEvent.click(within(row).getByRole('button', { name: 'Open the card of Multi' }));
    const dialog = await screen.findByRole('dialog');
    const card = dialog.querySelector('[data-file-card]') as HTMLElement;
    expect(within(card).getByTestId('plate-counter')).toHaveTextContent('1/3');
    await userEvent.click(within(card).getByRole('button', { name: 'Next plate' }));
    expect(within(card).getByTestId('plate-counter')).toHaveTextContent('2/3');
    expect(within(card).getByTestId('plate-materials')).toHaveTextContent('PETG+PLA');

    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await userEvent.click(within(row).getByTestId('plates-chip'));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });

  it('a single-plate row opens its card too - the same door for every file, without arrows', async () => {
    localStorage.setItem('library-view-mode', 'list');
    render(<FileManagerPage />);
    const row = await waitFor(() => surfaceOf('Single'), SETTLE);
    expect(within(row).queryByTestId('plates-chip')).not.toBeInTheDocument();
    await userEvent.click(within(row).getByRole('button', { name: 'Open the card of Single' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByTestId('plate-materials')).toHaveTextContent('ASA+PETG');
    expect(within(dialog).queryByRole('button', { name: 'Next plate' })).not.toBeInTheDocument();
  });
});
