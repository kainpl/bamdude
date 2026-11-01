import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { api, type QueueLeftover } from '../../api/client';
import { QueueLeftoversPanel } from '../../components/QueueLeftoversPanel';

const rights = vi.hoisted(() => ({ granted: new Set<string>() }));

vi.mock('../../contexts/AuthContext', async importOriginal => ({
  ...await importOriginal<typeof import('../../contexts/AuthContext')>(),
  useAuth: () => ({ authEnabled: true, user: { id: 1 }, hasPermission: (p: string) => rights.granted.has(p) }),
}));

const row = (overrides: Partial<QueueLeftover>): QueueLeftover => ({
  kind: 'finished_row', item_id: 26, auto_item_id: 73, printer_id: 7, printer_name: 'Printer 201',
  archive_id: 933, name: 'Lamp shade', completed_at: '2026-10-20T19:56:58Z',
  hold_reason: 'swap_hold_run_unproven', retirable: true, keep_reason: null, ...overrides,
});

const items: QueueLeftover[] = [
  row({}),
  row({ item_id: 41, auto_item_id: 84, name: 'Owed one', hold_reason: null, retirable: false, keep_reason: 'swap_owed' }),
  { kind: 'orphan_assignment', item_id: null, auto_item_id: 9, printer_id: null, printer_name: null, archive_id: null,
    name: null, completed_at: null, hold_reason: null, retirable: true, keep_reason: null },
];

describe('QueueLeftoversPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    rights.granted = new Set(['queue:read_all', 'queue:delete_all']);
    vi.spyOn(api, 'getQueueLeftovers').mockResolvedValue({ items });
  });

  it('is absent without the right to read every queue', () => {
    rights.granted = new Set();
    const { container } = render(<QueueLeftoversPanel />);
    expect(container).toBeEmptyDOMElement();
    expect(api.getQueueLeftovers).not.toHaveBeenCalled();
  });

  it('lists leftovers with the hold that kept them and why a row stays', async () => {
    render(<QueueLeftoversPanel />);
    expect(await screen.findAllByTestId('queue-leftover')).toHaveLength(3);
    expect(screen.getByText(/Printer 201 · Lamp shade · finished/)).toBeInTheDocument();
    expect(screen.getByText(/could not be tied to the print BamDude started/)).toBeInTheDocument();
    expect(screen.getByText('Kept: the table swap for this print is still owed.')).toBeInTheDocument();
    expect(screen.getByText('AutoQueue assignment #9 with no printer job')).toBeInTheDocument();
  });

  it('removes only what the server called retirable, after confirmation', async () => {
    const retire = vi.spyOn(api, 'retireQueueLeftovers').mockResolvedValue({
      retired_item_ids: [26], retired_orphan_ids: [9], skipped: [],
    });
    render(<QueueLeftoversPanel />);
    fireEvent.click(await screen.findByRole('button', { name: 'Remove (2)' }));
    expect(retire).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(dialog.querySelector('button:last-of-type') as HTMLButtonElement);
    await waitFor(() => expect(retire).toHaveBeenCalledWith({ item_ids: [26], orphan_ids: [9] }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('offers no removal without the right to delete every queue row', async () => {
    rights.granted = new Set(['queue:read_all']);
    render(<QueueLeftoversPanel />);
    expect(await screen.findAllByTestId('queue-leftover')).toHaveLength(3);
    expect(screen.queryByRole('button', { name: /Remove/ })).not.toBeInTheDocument();
  });
});
