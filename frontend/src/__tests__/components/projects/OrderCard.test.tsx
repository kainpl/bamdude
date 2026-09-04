import { describe, it, expect, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { render } from '../../utils';
import { strayZeroTextNodes } from '../../domHelpers';
import type { OrderListItem } from '../../../api/client';
import { OrderCard } from '../../../components/projects/OrderCard';

// Every entry of the card menu is permission-gated, and the render helper's
// real `AuthProvider` resolves an admin only once its own request has settled —
// which is after the synchronous clicks below. Only the hook is replaced; the
// provider itself stays real, so the tree mounts the way the app mounts it.
vi.mock('../../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../contexts/AuthContext')>();
  return { ...actual, useAuth: () => ({ ...actual.useAuth(), hasPermission: () => true }) };
});

const base: OrderListItem = { id: 1, name: 'Ten flasks', customer_id: 2, customer_name: 'ACME', color: '#00ae42', status: 'active', due_date: null, priority: 'normal', price: 120, tags: null, cover_image_filename: null, created_at: '2026-09-01T00:00:00Z', lines_count: 2, ordered: 10, printed: 4, progress: 0.4, line_products: [{ product_id: 11, has_cover: true }, { product_id: 12, has_cover: false }] };
const noop = () => {};

describe('OrderCard', () => {
  it('shows printed / ordered from the server and links to the order', () => {
    render(<OrderCard order={base} onEdit={noop} onDuplicate={noop} onSetStatus={noop} onDelete={noop} />);
    expect(screen.getByText('4 / 10')).toBeInTheDocument();
    expect(screen.getByRole('link')).toHaveAttribute('href', '/projects/1');
    // The anchor wraps no text any more (it is an overlay), so its accessible
    // name has to come from somewhere — otherwise the card is an unnamed link.
    expect(screen.getByRole('link')).toHaveAccessibleName('Ten flasks');
    expect(screen.getByText('ACME')).toBeInTheDocument();
    // One line's product has a cover, the other has none — the strip is one
    // tile per line either way, so the operator can see how many lines there are.
    expect(screen.getByTestId('product-cover')).toHaveAttribute('src', expect.stringContaining('/products/11/cover-image'));
    expect(screen.getAllByTestId('product-cover-placeholder')).toHaveLength(1);
  });
  it('shows at most three product tiles however long the order is', () => {
    const many = [11, 12, 13, 14, 15].map((product_id) => ({ product_id, has_cover: false }));
    render(<OrderCard order={{ ...base, line_products: many }} onEdit={noop} onDuplicate={noop} onSetStatus={noop} onDelete={noop} />);
    expect(screen.getAllByTestId('product-cover-placeholder')).toHaveLength(3);
  });
  it('an order with nothing ordered yet shows no bar and no stray zero', () => {
    render(<OrderCard order={{ ...base, ordered: 0, printed: 0, progress: 0, lines_count: 0, line_products: [] }} onEdit={noop} onDuplicate={noop} onSetStatus={noop} onDelete={noop} />);
    expect(screen.queryByTestId('order-1-progress')).not.toBeInTheDocument();
    // Scoped to the card: the rule is "a hidden bar leaves no bare 0 behind", not "the card never shows a zero" (pre-flight ruling 1).
    expect(strayZeroTextNodes(screen.getByTestId('order-1-card'))).toHaveLength(0);
  });
  it('flags an overdue active order', () => {
    render(<OrderCard order={{ ...base, due_date: '2020-01-01' }} onEdit={noop} onDuplicate={noop} onSetStatus={noop} onDelete={noop} />);
    expect(screen.getByText(/overdue/i)).toBeInTheDocument();
  });

  describe('actions menu', () => {
    /**
     * ⚠️ A `<button>` inside an `<a>` is invalid HTML, and the menu used to be
     * exactly that: every item cancelled the navigation its own click caused,
     * so one item added without the guard navigated instead of acting. The menu
     * now lives on `document.body` and the anchor is an overlay — nothing to
     * cancel, and nothing to forget.
     */
    it('renders the open menu outside the card anchor, on document.body', () => {
      render(<OrderCard order={base} onEdit={noop} onDuplicate={noop} onSetStatus={noop} onDelete={noop} />);

      fireEvent.click(screen.getByTestId('order-1-menu'));

      const panel = screen.getByRole('menu');
      expect(panel.parentElement).toBe(document.body);
      expect(screen.getByRole('link').contains(panel)).toBe(false);
      expect(screen.getByTestId('order-1-card').querySelector('[role="menu"]')).toBeNull();
    });

    it('acts on the item that was clicked, and closes', () => {
      const onEdit = vi.fn();
      render(<OrderCard order={base} onEdit={onEdit} onDuplicate={noop} onSetStatus={noop} onDelete={noop} />);

      fireEvent.click(screen.getByTestId('order-1-menu'));
      fireEvent.click(screen.getByRole('menuitem', { name: /edit/i }));

      expect(onEdit).toHaveBeenCalledWith(base);
      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });

    it('closes on Escape', () => {
      render(<OrderCard order={base} onEdit={noop} onDuplicate={noop} onSetStatus={noop} onDelete={noop} />);

      fireEvent.click(screen.getByTestId('order-1-menu'));
      expect(screen.getByRole('menu')).toBeInTheDocument();

      fireEvent.keyDown(window, { key: 'Escape' });
      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });

    it('names the trigger as a menu before it is opened', () => {
      render(<OrderCard order={base} onEdit={noop} onDuplicate={noop} onSetStatus={noop} onDelete={noop} />);

      const trigger = screen.getByTestId('order-1-menu');
      expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
      expect(trigger).toHaveAttribute('aria-expanded', 'false');

      fireEvent.click(trigger);
      expect(trigger).toHaveAttribute('aria-expanded', 'true');
    });
  });
});
