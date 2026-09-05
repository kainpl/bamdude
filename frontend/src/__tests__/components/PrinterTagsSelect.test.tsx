import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrinterTagsSelect } from '../../components/PrinterTagsSelect';

const TAGS = [
  { id: 1, name: 'Фаза 1', printer_count: 0, is_stagger_group: false },
  { id: 2, name: 'Фаза 2', printer_count: 0, is_stagger_group: false },
];

describe('PrinterTagsSelect', () => {
  it('shows chosen tags as chips and offers only the rest', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: TAGS })));
    const onChange = vi.fn();
    render(<PrinterTagsSelect value={[1]} onChange={onChange} />);
    expect(await screen.findByText('Фаза 1')).toBeInTheDocument();
    const select = screen.getByLabelText('Add a tag…') as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual(['Add a tag…', 'Фаза 2']);
    await userEvent.selectOptions(select, '2');
    expect(onChange).toHaveBeenCalledWith([1, 2]);
  });

  it('removes a chip', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: TAGS })));
    const onChange = vi.fn();
    render(<PrinterTagsSelect value={[1, 2]} onChange={onChange} />);
    await screen.findByText('Фаза 1');
    await userEvent.click(screen.getByRole('button', { name: 'Remove Фаза 1' }));
    expect(onChange).toHaveBeenCalledWith([2]);
  });

  it('creates inline and selects the new tag', async () => {
    server.use(
      http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: TAGS })),
      http.post('/api/v1/printer-tags', () => HttpResponse.json({ id: 9, name: 'Фаза 3' }, { status: 201 }))
    );
    const onChange = vi.fn();
    render(<PrinterTagsSelect value={[]} onChange={onChange} allowCreate />);
    await screen.findByLabelText('Add a tag…');
    await userEvent.click(screen.getByRole('button', { name: '+ New' }));
    await userEvent.type(screen.getByPlaceholderText('Add tag'), 'Фаза 3');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onChange).toHaveBeenCalledWith([9]);
  });

  it('tells the operator a duplicate name was refused, and keeps the draft', async () => {
    server.use(
      http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: TAGS })),
      http.post('/api/v1/printer-tags', () =>
        HttpResponse.json({ detail: 'A tag with this name already exists.' }, { status: 409 })
      )
    );
    const onChange = vi.fn();
    render(<PrinterTagsSelect value={[]} onChange={onChange} allowCreate />);
    await screen.findByLabelText('Add a tag…');
    await userEvent.click(screen.getByRole('button', { name: '+ New' }));
    const field = screen.getByPlaceholderText('Add tag');
    await userEvent.type(field, 'Фаза 1');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('A tag with this name already exists.')).toBeInTheDocument();
    // Nothing was added to the printer, and the name is still there to be edited.
    expect(onChange).not.toHaveBeenCalled();
    expect(field).toHaveValue('Фаза 1');
  });

  it('offers no inline create without allowCreate', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: TAGS })));
    render(<PrinterTagsSelect value={[]} onChange={vi.fn()} />);
    await screen.findByLabelText('Add a tag…');
    expect(screen.queryByRole('button', { name: '+ New' })).not.toBeInTheDocument();
  });
});
