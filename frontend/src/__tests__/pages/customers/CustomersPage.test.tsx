/**
 * `render` from `__tests__/utils` wraps in a BrowserRouter with no route
 * option — route-aware tests set the URL with pushState first, the way
 * `OrdersPage.test.tsx` does.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { CustomersPage } from '../../../pages/customers/CustomersPage';

const customers = [
  {
    id: 1,
    name: 'ACME',
    contact: 'acme@example.com',
    notes: null,
    figures: { projects: 3, active: 1, completed: 2, cancelled: 0, total_price: 450 },
  },
];

afterEach(() => {
  window.history.pushState({}, '', '/');
});

describe('CustomersPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('lists customers with their light figures', async () => {
    vi.spyOn(api, 'getCustomers').mockResolvedValue(customers as never);
    window.history.pushState({}, '', '/customers');
    render(<CustomersPage />);
    const row = (await screen.findByText('ACME')).closest('tr')!;
    expect(row.textContent).toContain('3'); // orders
    expect(row.textContent).toContain('450'); // total price
    expect(screen.getByRole('link', { name: 'ACME' })).toHaveAttribute('href', '/customers/1');
  });

  it('creates a customer through the modal', async () => {
    vi.spyOn(api, 'getCustomers').mockResolvedValue([] as never);
    const create = vi
      .spyOn(api, 'createCustomer')
      .mockResolvedValue({ id: 2, name: 'Bob', contact: null, notes: null, figures: {} } as never);
    window.history.pushState({}, '', '/customers');
    render(<CustomersPage />);
    fireEvent.click(await screen.findByRole('button', { name: /new customer/i }));
    fireEvent.change(screen.getByLabelText(/name/i), { target: { value: 'Bob' } });
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }));
    await waitFor(() => expect(create).toHaveBeenCalledWith({ name: 'Bob', contact: null, notes: null }));
  });
});
