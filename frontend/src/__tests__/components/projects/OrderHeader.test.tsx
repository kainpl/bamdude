/**
 * The order header's whole-order actions, and the one pass 8 added.
 *
 * «Bank the surplus» is DISABLED rather than hidden when there is nothing to
 * bank: it is a permanent action of the order, and a button that appears the
 * moment an overprint happens would arrive in the middle of a row nobody was
 * watching. Its enablement reads the figures the page already loaded — a
 * `surplus` on any part of any line — and asks the server nothing.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order, Permission } from '../../../api/client';
import { OrderHeader } from '../../../components/projects/OrderHeader';

const auth = vi.hoisted(() => ({ granted: null as Set<string> | null }));

vi.mock('../../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => {
      const real = actual.useAuth();
      return { ...real, hasPermission: (p: Permission) => auth.granted?.has(p) ?? true };
    },
  };
});

function orderWith(surplus: number): Order {
  return {
    id: 1,
    name: 'Ten flasks',
    customer_id: null,
    customer_name: null,
    status: 'active',
    priority: 'normal',
    price: null,
    tags: null,
    due_date: null,
    url: null,
    lines: [
      {
        id: 10,
        product_id: 1,
        product_name: 'Flask',
        quantity: 10,
        parts: [
          { part_id: 1, name: 'flask', qty_per_unit: 1, need: 10, usable: 10, in_progress: 0, remaining: 0, surplus: 0 },
          { part_id: 2, name: 'lid', qty_per_unit: 1, need: 10, usable: 10 + surplus, in_progress: 0, remaining: 0, surplus },
        ],
      },
    ],
    figures: { margin: null },
  } as unknown as Order;
}

const noop = () => {};

function mount(order: Order, onBankSurplus = noop) {
  render(
    <OrderHeader
      order={order}
      onEdit={noop}
      onDuplicate={noop}
      onDelete={noop}
      onSetStatus={noop}
      onBankSurplus={onBankSurplus}
      bankingSurplus={false}
    />,
  );
}

describe('OrderHeader · bank the surplus', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    auth.granted = null;
    vi.spyOn(api, 'getSettings').mockResolvedValue({ currency: 'USD' } as never);
  });

  it('is offered but refused while no line has overprinted', () => {
    mount(orderWith(0));

    const button = screen.getByTestId('order-bank-surplus');
    expect(button).toBeDisabled();
    // The button says why it cannot be pressed rather than leaving the operator
    // to guess which line it is waiting for.
    expect(button).toHaveAttribute('title', expect.stringMatching(/surplus/i));
  });

  it('is enabled by a surplus on any part of any line, and hands the press up', () => {
    const onBank = vi.fn();
    mount(orderWith(5), onBank);

    const button = screen.getByTestId('order-bank-surplus');
    expect(button).toBeEnabled();
    fireEvent.click(button);
    // ⚠️ The header does not POST: the page owns the call and the toast,
    // because only it knows which products the order's lines are for.
    expect(onBank).toHaveBeenCalledTimes(1);
  });

  it('is not offered to a reader', () => {
    auth.granted = new Set(['projects:read']);
    mount(orderWith(5));

    expect(screen.queryByTestId('order-bank-surplus')).not.toBeInTheDocument();
  });
});
