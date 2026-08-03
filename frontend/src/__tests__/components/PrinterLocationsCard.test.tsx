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
