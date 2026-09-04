/**
 * The two rules the checklist must not break: it disappears entirely when the
 * order buys nothing, and `remaining` is the SERVER's number — typing into
 * "acquired" patches and then waits, it never subtracts on screen. A checklist
 * that recomputes its own remainder tells the operator a different story from
 * the one the order page's figures tell, and only one of them is the truth.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order } from '../../../api/client';
import { ProcurementChecklist } from '../../../components/projects/ProcurementChecklist';

describe('ProcurementChecklist', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders nothing without purchased parts and PATCHes acquired on blur', async () => {
    const { rerender } = render(
      <ProcurementChecklist order={{ id: 1, procurement: [] } as unknown as Order} canEdit />,
    );
    expect(screen.queryByRole('table')).not.toBeInTheDocument();

    const patch = vi.spyOn(api, 'updateOrderProcurement').mockResolvedValue({} as Order);
    // ⚠️ `remaining` is deliberately NOT `need - acquired`. A self-consistent
    // fixture is passed by an implementation that quietly subtracts, which is
    // exactly the bug this test exists to catch: the server owns the number
    // (a part shared by two lines does not remain what the arithmetic says).
    rerender(
      <ProcurementChecklist
        order={
          {
            id: 1,
            procurement: [{ part_id: 4, name: 'M3 screw', need: 40, acquired: 10, remaining: 7 }],
          } as unknown as Order
        }
        canEdit
      />,
    );

    const input = screen.getByTestId('procurement-4-acquired') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '25' } });
    fireEvent.blur(input);

    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 4, 25));
    // Server value until refetch — never recomputed on the client.
    expect(screen.getByTestId('procurement-4-remaining').textContent).toBe('7');
  });

  it('puts the number back when the server refuses the commit', async () => {
    // The input is UNCONTROLLED and keyed on the server's `acquired`, so a
    // rejected PATCH re-renders nothing: without an explicit restore the box
    // sits there showing 25 while the server still holds 10, and the row reads
    // as saved for as long as the page stays open. The same argument the
    // component already makes for a cleared or negative field — "and the box
    // has to say so" — is what a 4xx needs too.
    const patch = vi
      .spyOn(api, 'updateOrderProcurement')
      .mockRejectedValue(new Error('Part not found'));

    render(
      <ProcurementChecklist
        order={
          {
            id: 1,
            procurement: [{ part_id: 4, name: 'M3 screw', need: 40, acquired: 10, remaining: 30 }],
          } as unknown as Order
        }
        canEdit
      />,
    );

    const input = () => screen.getByTestId('procurement-4-acquired') as HTMLInputElement;
    fireEvent.change(input(), { target: { value: '25' } });
    fireEvent.blur(input());

    await waitFor(() => expect(patch).toHaveBeenCalledWith(1, 4, 25));
    // The failure is reported...
    expect(await screen.findByText('Part not found')).toBeInTheDocument();
    // ...and the box no longer claims the number the server refused.
    await waitFor(() => expect(input().value).toBe('10'));
  });

  it('leaves the numbers read-only without the permission', () => {
    render(
      <ProcurementChecklist
        order={
          {
            id: 1,
            procurement: [{ part_id: 4, name: 'M3 screw', need: 40, acquired: 10, remaining: 30 }],
          } as unknown as Order
        }
        canEdit={false}
      />,
    );

    expect(screen.getByTestId('procurement-4-acquired')).toBeDisabled();
  });
});
