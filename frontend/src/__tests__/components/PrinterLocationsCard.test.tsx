import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { PrinterLocationsCard } from '../../components/settings/PrinterLocationsCard';
import { api } from '../../api/client';

const ROWS = {
  locations: [
    {
      id: 1,
      name: 'Workshop',
      parent_id: null,
      path: 'Workshop',
      depth: 1,
      printer_count: 2,
      sensor_count: 1,
      queued_count: 0,
    },
    {
      id: 2,
      name: 'Shelf 1',
      parent_id: 1,
      path: 'Workshop / Shelf 1',
      depth: 2,
      printer_count: 1,
      sensor_count: 0,
      queued_count: 0,
    },
  ],
};

describe('PrinterLocationsCard', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getPrinterLocations').mockResolvedValue(ROWS);
  });

  it('creates a location inside another', async () => {
    const create = vi
      .spyOn(api, 'createPrinterLocation')
      .mockResolvedValue({ id: 3, name: 'Shelf 2', parent_id: 1, path: 'Workshop / Shelf 2' });

    render(<PrinterLocationsCard />);
    await screen.findAllByText('Workshop');
    await userEvent.type(screen.getByPlaceholderText(/Workshop, Office/i), 'Shelf 2');
    await userEvent.selectOptions(screen.getByLabelText('Inside'), '1');
    await userEvent.click(screen.getByRole('button', { name: 'Add location' }));

    await waitFor(() => expect(create).toHaveBeenCalledWith('Shelf 2', 1));
  });

  it('moves a location back to the top level', async () => {
    // Without this a mistaken parent could only be fixed by deleting, and
    // deleting is refused while anything stands in the place.
    const update = vi.spyOn(api, 'updatePrinterLocation').mockResolvedValue(ROWS.locations[1]);

    render(<PrinterLocationsCard />);
    await screen.findAllByText('Shelf 1');
    await userEvent.selectOptions(screen.getByLabelText(/Shelf 1$/), '');

    await waitFor(() => expect(update).toHaveBeenCalledWith(2, { parent_id: null }));
  });

  it('shows the reason the backend gives rather than guessing at one', async () => {
    // A cycle and a fourth level each say what is wrong; "name taken" would be
    // a guess, and usually the wrong one.
    vi.spyOn(api, 'updatePrinterLocation').mockRejectedValue(
      new Error('A location cannot be placed inside itself.'),
    );

    render(<PrinterLocationsCard />);
    await screen.findAllByText('Shelf 1');
    await userEvent.selectOptions(screen.getByLabelText(/Shelf 1$/), '');

    expect(await screen.findByText(/cannot be placed inside itself/i)).toBeInTheDocument();
  });

  it('does not offer a location as its own parent', async () => {
    render(<PrinterLocationsCard />);
    await screen.findAllByText('Shelf 1');

    const own = screen.getByLabelText(/Shelf 1$/) as HTMLSelectElement;
    expect([...own.options].map((option) => option.value)).not.toContain('2');
  });
});

describe('the rows line up as columns', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getPrinterLocations').mockResolvedValue(ROWS);
  });

  it('shares one grid between all rows rather than a flex row each', async () => {
    // ⚠️ The bug this replaces: every column here is content-sized, so with a
    // flex row per item each row sized its own. A row whose parent read "Top
    // level" pushed its picker wider and further left than the row below it.
    render(<PrinterLocationsCard />);
    await screen.findAllByText('Shelf 1');

    const list = screen.getByRole('list');
    expect(list.className).toContain('grid');
    expect(list.className).toContain('grid-cols-');
  });

  it('lets each row hand its cells to that grid', async () => {
    render(<PrinterLocationsCard />);
    await screen.findAllByText('Shelf 1');

    for (const row of screen.getAllByRole('listitem')) {
      expect(row.className).toContain('contents');
    }
  });

  it('gives every row exactly one cell per column', async () => {
    // ⚠️ This is the trap `contents` sets. Every child of the row becomes a
    // grid item — including one that is only there for screen readers. Adding a
    // sibling `<label class="sr-only">` back would take a column of its own and
    // push the whole row one cell across, which is why the picker is labelled
    // by `aria-label` instead.
    render(<PrinterLocationsCard />);
    await screen.findAllByText('Shelf 1');

    const columns = (screen.getByRole('list').className.match(/grid-cols-\[(.+?)\]/) || [])[1];
    expect(columns?.split('_')).toHaveLength(4);
    for (const row of screen.getAllByRole('listitem')) {
      expect(row.children).toHaveLength(4);
    }
  });

  it('still names the picker for a screen reader', async () => {
    render(<PrinterLocationsCard />);
    await screen.findAllByText('Shelf 1');

    expect(screen.getByLabelText(/Shelf 1$/)).toBeInstanceOf(HTMLSelectElement);
  });
});
