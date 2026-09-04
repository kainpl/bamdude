/**
 * The field that decides which ORDER a print is filed under.
 *
 * The picker is the only place an operator says "this plate is for that order"
 * outside the order's own plan block, so three things have to hold: the list
 * arrives ranked and must not be re-sorted here, a satisfied line stays
 * choosable (printing ahead is legitimate), and choosing carries BOTH ids —
 * the order and the line — because a line without its order names nothing.
 */

import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { strayZeroTextNodes } from '../domHelpers';
import { OrderFilingField } from '../../components/OrderFilingField';
import type { OrderCandidate } from '../../api/client';

const candidate = (over: Partial<OrderCandidate> = {}): OrderCandidate => ({
  project_id: 4,
  project_name: 'Kickstarter batch',
  project_line_id: 9,
  product_id: 2,
  product_name: 'Desk Lamp',
  outstanding_prints: 5,
  priority: 2,
  deadline: null,
  created_at: '2026-09-01T10:14:02',
  ...over,
});

describe('OrderFilingField', () => {
  it('offers «Without an order» first, then the candidates in the order given', async () => {
    render(
      <OrderFilingField
        value={null}
        onChange={vi.fn()}
        candidates={[
          candidate(),
          candidate({ project_id: 6, project_name: 'Spare stock', project_line_id: 12, outstanding_prints: 0 }),
        ]}
      />,
    );

    const options = screen.getAllByRole('option').map((o) => o.textContent);
    expect(options).toEqual([
      'Without an order',
      'Kickstarter batch — Desk Lamp · still needs 5 prints',
      'Spare stock — Desk Lamp · already covered',
    ]);
  });

  it('uses the singular form for a line that needs one more print', () => {
    render(<OrderFilingField value={null} onChange={vi.fn()} candidates={[candidate({ outstanding_prints: 1 })]} />);

    expect(
      screen.getByRole('option', { name: 'Kickstarter batch — Desk Lamp · still needs 1 print' }),
    ).toBeInTheDocument();
  });

  it('carries both ids when a candidate is chosen', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <OrderFilingField
        value={null}
        onChange={onChange}
        candidates={[candidate(), candidate({ project_id: 6, project_line_id: 12, project_name: 'Spare stock' })]}
      />,
    );

    await user.selectOptions(screen.getByLabelText('Order'), '6:12');

    expect(onChange).toHaveBeenCalledWith({ projectId: 6, projectLineId: 12 });
  });

  it('carries null back when the operator picks «Without an order»', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <OrderFilingField
        value={{ projectId: 4, projectLineId: 9 }}
        onChange={onChange}
        candidates={[candidate()]}
      />,
    );

    await user.selectOptions(screen.getByLabelText('Order'), '');

    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('shows the current value as the selected option', () => {
    render(
      <OrderFilingField
        value={{ projectId: 6, projectLineId: 12 }}
        onChange={vi.fn()}
        candidates={[candidate(), candidate({ project_id: 6, project_line_id: 12, project_name: 'Spare stock' })]}
      />,
    );

    expect((screen.getByLabelText('Order') as HTMLSelectElement).value).toBe('6:12');
  });

  it('renders nothing at all when no order wants this plate', () => {
    render(<OrderFilingField value={null} onChange={vi.fn()} candidates={[]} />);

    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.queryByText('Order')).not.toBeInTheDocument();
  });

  it('renders nothing while the candidates are still being fetched', () => {
    render(<OrderFilingField value={null} onChange={vi.fn()} candidates={undefined} loading />);

    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('leaves no stray zero where a count of 0 is rendered', () => {
    render(<OrderFilingField value={null} onChange={vi.fn()} candidates={[candidate({ outstanding_prints: 0 })]} />);

    expect(strayZeroTextNodes()).toHaveLength(0);
  });
});
