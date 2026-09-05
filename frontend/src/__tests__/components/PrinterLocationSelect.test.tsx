import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrinterLocationSelect } from '../../components/PrinterLocationSelect';

const LOCATIONS = [
  { id: 1, name: 'Цех A', parent_id: null, path: 'Цех A', depth: 1, printer_count: 2, sensor_count: 0, queued_count: 0 },
  {
    id: 2,
    name: 'Ряд 1',
    parent_id: 1,
    path: 'Цех A / Ряд 1',
    depth: 2,
    printer_count: 1,
    sensor_count: 0,
    queued_count: 0,
  },
];

describe('PrinterLocationSelect', () => {
  it('creates inline and selects the new location', async () => {
    server.use(
      http.get('/api/v1/printer-locations', () => HttpResponse.json({ locations: LOCATIONS })),
      http.post('/api/v1/printer-locations', () =>
        HttpResponse.json({ id: 9, name: 'Ряд 2', parent_id: null, path: 'Ряд 2' }, { status: 201 })
      )
    );
    const onChange = vi.fn();
    render(<PrinterLocationSelect value={null} onChange={onChange} allowCreate />);
    await screen.findByText('Ungrouped');
    await userEvent.click(screen.getByRole('button', { name: '+ New' }));
    await userEvent.type(screen.getByPlaceholderText('e.g., Workshop, Office, Basement'), 'Ряд 2');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(onChange).toHaveBeenCalledWith(9);
  });

  /**
   * The sibling of ``PrinterTagsSelect``'s duplicate-name case, and it exists
   * for the same reason: without an `onError` the refusal lands nowhere, and
   * the row stays open with a Save button that looks live but has already been
   * pressed — which reads as a broken form rather than as a name already taken.
   */
  it('tells the operator a duplicate name was refused, and keeps the draft', async () => {
    server.use(
      http.get('/api/v1/printer-locations', () => HttpResponse.json({ locations: LOCATIONS })),
      http.post('/api/v1/printer-locations', () =>
        HttpResponse.json({ detail: 'A location with this name already exists.' }, { status: 409 })
      )
    );
    const onChange = vi.fn();
    render(<PrinterLocationSelect value={null} onChange={onChange} allowCreate />);
    await screen.findByText('Ungrouped');
    await userEvent.click(screen.getByRole('button', { name: '+ New' }));
    const field = screen.getByPlaceholderText('e.g., Workshop, Office, Basement');
    await userEvent.type(field, 'Цех A');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('A location with this name already exists.')).toBeInTheDocument();
    // Nothing was assigned to the printer, and the name is still there to be edited.
    expect(onChange).not.toHaveBeenCalled();
    expect(field).toHaveValue('Цех A');
  });
});
