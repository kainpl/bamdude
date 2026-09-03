/**
 * Filing one archive under an order, and then under one of its lines.
 *
 * The ArchivesPage carried two copies of this menu, each filtering
 * `status === 'active'` inline — stricter than the shared rule, so an archive
 * already bound to a closed order could not even be seen to be bound. One
 * component now, and the rule comes from `selectableProjects`.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Archive } from '../../../api/client';
import { AddToOrderMenu } from '../../../components/projects/AddToOrderMenu';

describe('AddToOrderMenu', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('offers active orders and the bound one only', async () => {
    vi.spyOn(api, 'getOrders').mockResolvedValue([
      { id: 1, name: 'Open', status: 'active' },
      { id: 2, name: 'Done', status: 'completed' },
      { id: 3, name: 'Mine', status: 'cancelled' },
    ] as never);

    render(<AddToOrderMenu archive={{ id: 9, project_id: 3 } as Archive} onDone={() => {}} />);

    expect(await screen.findByText('Open')).toBeInTheDocument();
    expect(screen.getByText('Mine')).toBeInTheDocument();
    expect(screen.queryByText('Done')).not.toBeInTheDocument();
  });

  it('files the archive under the picked line of the picked order', async () => {
    vi.spyOn(api, 'getOrders').mockResolvedValue([{ id: 1, name: 'Open', status: 'active' }] as never);
    vi.spyOn(api, 'getOrder').mockResolvedValue({
      id: 1,
      lines: [{ id: 10, product_name: 'Flask', quantity: 2, material: null }],
    } as never);
    const add = vi.spyOn(api, 'addArchivesToOrder').mockResolvedValue({} as never);
    const onDone = vi.fn();

    render(<AddToOrderMenu archive={{ id: 9, project_id: null } as Archive} onDone={onDone} />);

    fireEvent.click(await screen.findByText('Open'));
    fireEvent.click(await screen.findByText('Flask × 2'));

    await waitFor(() => expect(add).toHaveBeenCalledWith(1, [9], 10));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });

  it('files the archive under no line when the order is enough', async () => {
    vi.spyOn(api, 'getOrders').mockResolvedValue([{ id: 1, name: 'Open', status: 'active' }] as never);
    vi.spyOn(api, 'getOrder').mockResolvedValue({ id: 1, lines: [] } as never);
    const add = vi.spyOn(api, 'addArchivesToOrder').mockResolvedValue({} as never);

    render(<AddToOrderMenu archive={{ id: 9, project_id: null } as Archive} onDone={() => {}} />);

    fireEvent.click(await screen.findByText('Open'));
    fireEvent.click(await screen.findByRole('button', { name: /no line/i }));

    await waitFor(() => expect(add).toHaveBeenCalledWith(1, [9], null));
  });

  it('offers to unbind an archive that is already in an order', async () => {
    vi.spyOn(api, 'getOrders').mockResolvedValue([{ id: 1, name: 'Open', status: 'active' }] as never);
    const patch = vi.spyOn(api, 'updateArchive').mockResolvedValue({} as never);

    render(<AddToOrderMenu archive={{ id: 9, project_id: 1 } as Archive} onDone={() => {}} />);

    fireEvent.click(await screen.findByRole('button', { name: /remove from order/i }));

    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith(9, { project_id: null, project_line_id: null }),
    );
  });
});
