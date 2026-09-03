import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { OrderLinePicker } from '../../../components/pickers/OrderLinePicker';

const order = { id: 5, lines: [
  { id: 11, product_name: 'Flask', quantity: 2, material: 'PETG' },
  { id: 12, product_name: 'Lid', quantity: 4, material: null },
] };

describe('OrderLinePicker', () => {
  beforeEach(() => vi.restoreAllMocks());
  it('is disabled until an order is chosen', () => {
    render(<OrderLinePicker orderId={null} value={null} onChange={() => {}} />);
    expect(screen.getByRole('combobox')).toBeDisabled();
  });
  it('lists the chosen order\'s lines with product, quantity and material', async () => {
    vi.spyOn(api, 'getOrder').mockResolvedValue(order as never);
    const onChange = vi.fn();
    render(<OrderLinePicker orderId={5} value={null} onChange={onChange} />);
    expect(await screen.findByRole('option', { name: 'Flask × 2 [PETG]' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Lid × 4' })).toBeInTheDocument();
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '12' } });
    expect(onChange).toHaveBeenCalledWith(12);
  });
});
