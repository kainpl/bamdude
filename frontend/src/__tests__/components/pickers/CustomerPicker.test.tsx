import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { CustomerPicker } from '../../../components/pickers/CustomerPicker';

const figures = { projects: 0, active: 0, completed: 0, cancelled: 0, total_price: 0 };
const customers = [
  { id: 1, name: 'Acme', contact: null, notes: null, created_at: '', updated_at: '', figures },
];

describe('CustomerPicker', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('renders "no customer" first', async () => {
    vi.spyOn(api, 'getCustomers').mockResolvedValue(customers as never);
    render(<CustomerPicker value={null} onChange={() => {}} />);
    const options = await screen.findAllByRole('option');
    expect(options[0]).toHaveTextContent('No customer');
  });

  it('choosing "new customer…" shows a name input, and submitting creates the customer', async () => {
    vi.spyOn(api, 'getCustomers').mockResolvedValue(customers as never);
    const create = vi.spyOn(api, 'createCustomer').mockResolvedValue({ id: 7, name: 'Beta' } as never);
    const onChange = vi.fn();
    render(<CustomerPicker value={null} onChange={onChange} allowCreate />);
    const select = await screen.findByRole('combobox');
    fireEvent.change(select, { target: { value: screen.getByRole('option', { name: /new customer/i }).getAttribute('value') } });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Beta' } });
    fireEvent.click(screen.getByRole('button', { name: /create/i }));
    await waitFor(() => expect(create).toHaveBeenCalledWith({ name: 'Beta' }));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(7));
  });

  it('honours disabled once the create-name view is showing', async () => {
    vi.spyOn(api, 'getCustomers').mockResolvedValue(customers as never);
    const create = vi.spyOn(api, 'createCustomer').mockResolvedValue({ id: 8, name: 'Gamma' } as never);
    const { rerender } = render(<CustomerPicker value={null} onChange={() => {}} allowCreate />);
    const select = await screen.findByRole('combobox');
    fireEvent.change(select, {
      target: { value: screen.getByRole('option', { name: /new customer/i }).getAttribute('value') },
    });
    rerender(<CustomerPicker value={null} onChange={() => {}} allowCreate disabled />);
    expect(screen.getByRole('textbox')).toBeDisabled();
    const createButton = screen.getByRole('button', { name: /create/i });
    expect(createButton).toBeDisabled();
    fireEvent.click(createButton);
    expect(create).not.toHaveBeenCalled();
  });
});
