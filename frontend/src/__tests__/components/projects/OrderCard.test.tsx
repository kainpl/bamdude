import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../../utils';
import { strayZeroTextNodes } from '../../domHelpers';
import { OrderCard } from '../../../components/projects/OrderCard';

const base = { id: 1, name: 'Ten flasks', customer_id: 2, customer_name: 'ACME', color: '#00ae42', status: 'active', due_date: null, priority: 'normal', price: 120, tags: null, cover_image_filename: null, created_at: '2026-09-01T00:00:00Z', lines_count: 2, ordered: 10, printed: 4, progress: 0.4, product_cover_filenames: [null, null] } as const;
const noop = () => {};

describe('OrderCard', () => {
  it('shows printed / ordered from the server and links to the order', () => {
    render(<OrderCard order={base} onEdit={noop} onDuplicate={noop} onSetStatus={noop} onDelete={noop} />);
    expect(screen.getByText('4 / 10')).toBeInTheDocument();
    expect(screen.getByRole('link')).toHaveAttribute('href', '/projects/1');
    expect(screen.getByText('ACME')).toBeInTheDocument();
    expect(screen.getAllByTestId('product-cover-placeholder')).toHaveLength(2);
  });
  it('an order with nothing ordered yet shows no bar and no stray zero', () => {
    render(<OrderCard order={{ ...base, ordered: 0, printed: 0, progress: 0, lines_count: 0, product_cover_filenames: [] }} onEdit={noop} onDuplicate={noop} onSetStatus={noop} onDelete={noop} />);
    expect(screen.queryByTestId('order-1-progress')).not.toBeInTheDocument();
    // Scoped to the card: the rule is "a hidden bar leaves no bare 0 behind", not "the card never shows a zero" (pre-flight ruling 1).
    expect(strayZeroTextNodes(screen.getByTestId('order-1-card'))).toHaveLength(0);
  });
  it('flags an overdue active order', () => {
    render(<OrderCard order={{ ...base, due_date: '2020-01-01' }} onEdit={noop} onDuplicate={noop} onSetStatus={noop} onDelete={noop} />);
    expect(screen.getByText(/overdue/i)).toBeInTheDocument();
  });
});
