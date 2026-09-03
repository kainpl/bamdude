import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { ProductPicker } from '../../../components/pickers/ProductPicker';

const products = [
  { id: 1, name: 'Flask', is_active: true },
  { id: 2, name: 'Old lid', is_active: false },
];

describe('ProductPicker', () => {
  beforeEach(() => vi.restoreAllMocks());
  it('offers catalog products and hides inactive ones unless bound', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue(products as never);
    const { rerender } = render(<ProductPicker value={null} onChange={() => {}} />);
    expect(await screen.findByRole('button', { name: 'Flask' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Old lid' })).not.toBeInTheDocument();
    rerender(<ProductPicker value={2} onChange={() => {}} />);
    expect(await screen.findByRole('button', { name: 'Old lid' })).toBeInTheDocument();
  });
  it('creates a product from the typed name when nothing matches', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue(products as never);
    const create = vi.spyOn(api, 'createProduct').mockResolvedValue({ id: 9, name: 'Cap' } as never);
    const onChange = vi.fn();
    render(<ProductPicker value={null} onChange={onChange} allowCreate />);
    fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'Cap' } });
    fireEvent.click(screen.getByRole('button', { name: /create/i }));
    await waitFor(() => expect(create).toHaveBeenCalledWith({ name: 'Cap' }));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(9));
  });
  it('honours disabled while offering to create a product', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue(products as never);
    const create = vi.spyOn(api, 'createProduct').mockResolvedValue({ id: 10, name: 'Mug' } as never);
    render(<ProductPicker value={null} onChange={() => {}} allowCreate disabled />);
    const input = await screen.findByRole('textbox');
    expect(input).toBeDisabled();
    fireEvent.change(input, { target: { value: 'Mug' } });
    const createButton = await screen.findByRole('button', { name: /create/i });
    expect(createButton).toBeDisabled();
    fireEvent.click(createButton);
    expect(create).not.toHaveBeenCalled();
  });
});
