/**
 * `render` from `__tests__/utils` wraps in a BrowserRouter with no route
 * option — route-aware tests set the URL with pushState first, the way
 * `OrdersPage.test.tsx` does.
 *
 * The page asks `getCustomersPaged` (spec projects-lists-parity): search,
 * sort and page live in the URL; the view mode and page size are preferences.
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

const pageOf = (items: unknown[], meta: Partial<{ total: number; current_page: number; per_page: number; last_page: number }> = {}) =>
  ({ items, meta: { total: items.length, current_page: 1, per_page: 24, last_page: 1, ...meta } }) as never;

afterEach(() => {
  window.history.pushState({}, '', '/');
});

describe('CustomersPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('lists customers with their light figures, one page by name', async () => {
    const get = vi.spyOn(api, 'getCustomersPaged').mockResolvedValue(pageOf(customers));
    window.history.pushState({}, '', '/customers');
    render(<CustomersPage />);
    const row = (await screen.findByText('ACME')).closest('tr')!;
    expect(row.textContent).toContain('3'); // orders
    // total price, through the shared money formatter — symbol in front, two decimals
    expect(row.textContent).toContain('$450.00');
    expect(screen.getByRole('link', { name: 'ACME' })).toHaveAttribute('href', '/customers/1');
    expect(get).toHaveBeenLastCalledWith({ sort_by: 'name-asc', page: 1, per_page: 24 });
  });

  it('searches from the URL and into it', async () => {
    const get = vi.spyOn(api, 'getCustomersPaged').mockResolvedValue(pageOf(customers));
    window.history.pushState({}, '', '/customers?q=acme&page=2');
    render(<CustomersPage />);
    await waitFor(() => expect(get).toHaveBeenLastCalledWith({ q: 'acme', sort_by: 'name-asc', page: 2, per_page: 24 }));
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'x.ua' } });
    await waitFor(() => expect(get).toHaveBeenLastCalledWith({ q: 'x.ua', sort_by: 'name-asc', page: 1, per_page: 24 }));
    expect(window.location.search).toBe('?q=x.ua');
  });

  it('switches to cards, remembers it, and a card shows the same figures', async () => {
    vi.spyOn(api, 'getCustomersPaged').mockResolvedValue(pageOf(customers));
    window.history.pushState({}, '', '/customers');
    render(<CustomersPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Cards' }));
    const card = await screen.findByTestId('customer-1-card');
    expect(card).toHaveTextContent('acme@example.com');
    expect(card).toHaveTextContent('3 orders');
    expect(card).toHaveTextContent('$450.00');
    expect(localStorage.getItem('bamdude-customers-view')).toBe('cards');
  });

  it('sorts by a column on the server', async () => {
    const get = vi.spyOn(api, 'getCustomersPaged').mockResolvedValue(pageOf(customers));
    window.history.pushState({}, '', '/customers');
    render(<CustomersPage />);
    await screen.findByText('ACME');
    fireEvent.click(screen.getByRole('button', { name: /Total price/ }));
    await waitFor(() => expect(get).toHaveBeenLastCalledWith(expect.objectContaining({ sort_by: 'total_price-desc' })));
    expect(window.location.search).toContain('sort=total_price-desc');
  });

  it('an empty search offers to reset it', async () => {
    const get = vi.spyOn(api, 'getCustomersPaged').mockResolvedValue(pageOf([]));
    window.history.pushState({}, '', '/customers?q=zzz');
    render(<CustomersPage />);
    expect(await screen.findByText('Nothing matches your search or filters.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Reset' }));
    await waitFor(() => expect(get).toHaveBeenLastCalledWith({ sort_by: 'name-asc', page: 1, per_page: 24 }));
  });

  it('creates a customer through the modal', async () => {
    vi.spyOn(api, 'getCustomersPaged').mockResolvedValue(pageOf([]));
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
  it('in cards, sorts from the toolbar — the key and both directions', async () => {
    localStorage.setItem('bamdude-customers-view', 'cards');
    const get = vi.spyOn(api, 'getCustomersPaged').mockResolvedValue(pageOf(customers));
    window.history.pushState({}, '', '/customers');
    render(<CustomersPage />);
    await screen.findByTestId('customer-1-card');
    fireEvent.change(screen.getByLabelText('Sort by'), { target: { value: 'total_price' } });
    await waitFor(() => expect(get).toHaveBeenLastCalledWith(expect.objectContaining({ sort_by: 'total_price-desc', page: 1 })));
    fireEvent.click(screen.getByRole('button', { name: 'Descending' }));
    await waitFor(() => expect(get).toHaveBeenLastCalledWith(expect.objectContaining({ sort_by: 'total_price-asc' })));
  });

  it('in the table, the page bar sits inside the table card', async () => {
    vi.spyOn(api, 'getCustomersPaged').mockResolvedValue(pageOf(customers, { total: 30, last_page: 2 }));
    window.history.pushState({}, '', '/customers');
    render(<CustomersPage />);
    const range = await screen.findByText('Showing 1-24 of 30 customers');
    expect(range.closest('.rounded-xl')?.querySelector('table')).toBeTruthy();
  });

  it('marks the list busy while the next page is on its way', async () => {
    let release: () => void = () => {};
    vi.spyOn(api, 'getCustomersPaged').mockImplementation((params) =>
      params.page === 2
        ? new Promise((r) => {
            release = () => r(pageOf(customers, { total: 30, last_page: 2, current_page: 2 }));
          })
        : Promise.resolve(pageOf(customers, { total: 30, last_page: 2 })),
    );
    window.history.pushState({}, '', '/customers');
    render(<CustomersPage />);
    await screen.findByText('ACME');
    expect(screen.getByTestId('list-body')).toHaveAttribute('aria-busy', 'false');
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(screen.getByTestId('list-body')).toHaveAttribute('aria-busy', 'true'));
    release();
    await waitFor(() => expect(screen.getByTestId('list-body')).toHaveAttribute('aria-busy', 'false'));
  });

  it('Back to a later page is not clamped by the previous answer still on screen', async () => {
    let release: () => void = () => {};
    const get = vi.spyOn(api, 'getCustomersPaged').mockImplementation((params) =>
      params.page === 3
        ? new Promise((r) => {
            release = () => r(pageOf(customers, { total: 60, last_page: 3, current_page: 3 }));
          })
        : Promise.resolve(pageOf(customers)),
    );
    window.history.pushState({}, '', '/customers?q=acme');
    render(<CustomersPage />);
    await screen.findByText('ACME');
    window.history.pushState({}, '', '/customers?page=3');
    window.dispatchEvent(new PopStateEvent('popstate'));
    await waitFor(() => expect(get).toHaveBeenLastCalledWith(expect.objectContaining({ page: 3 })));
    release();
    expect(await screen.findByText('Showing 49-60 of 60 customers')).toBeInTheDocument();
    expect(window.location.search).toContain('page=3');
  });
});
