import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { useQueryClient } from '@tanstack/react-query';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { ProductPicker } from '../../../components/pickers/ProductPicker';

const products = [
  { id: 1, name: 'Flask', is_active: true },
  { id: 2, name: 'Old lid', is_active: false },
];

/** Somebody retires a product in another tab: the catalog query is invalidated
 *  farm-wide and comes back with the row flagged inactive. */
function Retire() {
  const queryClient = useQueryClient();
  return (
    <button type="button" data-testid="retire" onClick={() => queryClient.invalidateQueries({ queryKey: ['products'] })}>
      retire
    </button>
  );
}

describe('ProductPicker', () => {
  beforeEach(() => vi.restoreAllMocks());
  it('offers catalog products and hides inactive ones unless bound', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue(products as never);
    render(<ProductPicker value={null} onChange={() => {}} />);
    expect(await screen.findByRole('button', { name: 'Flask' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Old lid' })).not.toBeInTheDocument();
  });
  it('keeps a product the picker ARRIVED bound to, retired or not', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue(products as never);
    render(<ProductPicker value={2} onChange={() => {}} />);
    expect(await screen.findByRole('button', { name: 'Old lid' })).toBeInTheDocument();
    expect(screen.getByText(/not in catalog/i)).toBeInTheDocument();
  });
  it('a product retired AFTER mount stays on offer', async () => {
    // ⚠️ The keep-set is frozen at mount, the same rule `LinkToProductsModal`
    // uses. Read live instead, the binding would vanish from the field the
    // moment somebody retired the product elsewhere — and the next save would
    // write that emptiness back.
    const get = vi
      .spyOn(api, 'getProducts')
      .mockResolvedValueOnce([{ id: 1, name: 'Flask', is_active: true }] as never)
      .mockResolvedValue([{ id: 1, name: 'Flask', is_active: false }] as never);

    render(
      <>
        <Retire />
        <ProductPicker value={1} onChange={() => {}} />
      </>,
    );

    expect(await screen.findByRole('button', { name: 'Flask' })).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('retire'));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));

    expect(await screen.findByText(/not in catalog/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Flask' })).toBeInTheDocument();
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
