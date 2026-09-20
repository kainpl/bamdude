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

  it('Escape in the name field steps back to the select without creating anything', async () => {
    // Picking "new customer…" by accident used to be a one-way door: the only
    // way back was to create a customer nobody wanted.
    vi.spyOn(api, 'getCustomers').mockResolvedValue(customers as never);
    const create = vi.spyOn(api, 'createCustomer').mockResolvedValue({ id: 9, name: 'Delta' } as never);
    render(<CustomerPicker value={null} onChange={() => {}} allowCreate />);
    const select = await screen.findByRole('combobox');
    fireEvent.change(select, {
      target: { value: screen.getByRole('option', { name: /new customer/i }).getAttribute('value') },
    });

    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'Delta' } });
    fireEvent.keyDown(input, { key: 'Escape' });

    expect(await screen.findByRole('combobox')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(create).not.toHaveBeenCalled();
  });

  it('the × beside Create goes back and forgets what was typed', async () => {
    vi.spyOn(api, 'getCustomers').mockResolvedValue(customers as never);
    const create = vi.spyOn(api, 'createCustomer').mockResolvedValue({ id: 9, name: 'Delta' } as never);
    render(<CustomerPicker value={null} onChange={() => {}} allowCreate />);
    const select = await screen.findByRole('combobox');
    fireEvent.change(select, {
      target: { value: screen.getByRole('option', { name: /new customer/i }).getAttribute('value') },
    });

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Delta' } });
    fireEvent.click(screen.getByRole('button', { name: /cancel creating customer/i }));

    expect(await screen.findByRole('combobox')).toBeInTheDocument();
    expect(create).not.toHaveBeenCalled();

    // Back in again: the abandoned name is gone, so Create is not offered.
    fireEvent.change(screen.getByRole('combobox'), {
      target: { value: screen.getByRole('option', { name: /new customer/i }).getAttribute('value') },
    });
    expect(screen.getByRole('textbox')).toHaveValue('');
  });
  it('names its Cancel for what it cancels, not just "Cancel"', async () => {
    // ⚠️ The picker lives INSIDE dialogs that have a Cancel of their own. Two
    // buttons called "Cancel" in one form is a coin toss for anybody driving
    // it by accessible name — a screen reader, a keyboard user reading the
    // rotor, or a test.
    vi.spyOn(api, 'getCustomers').mockResolvedValue(customers as never);
    render(<CustomerPicker value={null} onChange={() => {}} allowCreate />);
    const select = await screen.findByRole('combobox');
    fireEvent.change(select, {
      target: { value: screen.getByRole('option', { name: /new customer/i }).getAttribute('value') },
    });

    expect(screen.getByRole('button', { name: /cancel creating customer/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^cancel$/i })).not.toBeInTheDocument();
  });
});
