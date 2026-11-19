/**
 * What the archive editor may do to a print's status and printer (upstream audit G04).
 *
 * - «Cancelled» stores `cancelled` — it used to store `aborted`, which statistics
 *   count as a physical failure.
 * - The dialog opens on the print's own status, whatever it is; a status it does
 *   not offer is shown as itself and nothing is sent unless it is changed.
 * - `printing` is never a target, and a running print's status is not editable.
 * - The printer is a fact of the run: no control, and never in the request.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { EditArchiveModal } from '../../components/EditArchiveModal';
import type { Archive } from '../../api/client';

const base = {
  id: 7,
  printer_id: 3,
  filename: 'a.gcode.3mf',
  print_name: 'A',
  status: 'completed',
  extra_data: null,
  external_url: null,
  is_favorite: false,
  tags: '',
  notes: '',
  cost: null,
  photos: null,
  failure_reason: null,
  error_message: null,
  quantity: 1,
  defective_count: 0,
  parts: [{ id: 1, name: 'lid', name_key: 'lid', quantity: 1, defective: 0 }],
  created_at: '2026-11-01T00:00:00Z',
  created_by_id: null,
} as unknown as Archive;

const sent: Record<string, unknown>[] = [];

function open(overrides: Partial<Archive> = {}) {
  return render(<EditArchiveModal archive={{ ...base, ...overrides } as Archive} onClose={vi.fn()} existingTags={['x']} />);
}

function statusSelect(): HTMLSelectElement {
  return screen.getByLabelText('Status') as HTMLSelectElement;
}

async function save() {
  await userEvent.click(screen.getByRole('button', { name: /save/i }));
  await waitFor(() => expect(sent).toHaveLength(1));
  return sent[0];
}

describe('EditArchiveModal — status and printer', () => {
  beforeEach(() => {
    sent.length = 0;
    server.use(
      http.get('/api/v1/projects/', () =>
        HttpResponse.json({ items: [], meta: { total: 0, current_page: 1, per_page: 20, last_page: 1 } }),
      ),
      http.patch('/api/v1/archives/:id', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        sent.push(body);
        return HttpResponse.json({ ...base, ...body });
      }),
    );
  });

  it('offers the four terminal statuses under their own names, and never «Printing»', () => {
    open();
    const options = [...statusSelect().options].map((o) => [o.value, o.textContent]);
    expect(options).toEqual([
      ['completed', 'Completed'],
      ['failed', 'Failed'],
      ['aborted', 'Aborted'],
      ['cancelled', 'Cancelled'],
    ]);
  });

  it('saves «Cancelled» as cancelled', async () => {
    open({ status: 'failed', failure_reason: 'filamentRunout' });
    await userEvent.selectOptions(statusSelect(), 'Cancelled');
    const body = await save();
    expect(body.status).toBe('cancelled');
    // What was recorded stays with a cancelled print.
    expect(body).not.toHaveProperty('failure_reason');
  });

  it('opens a cancelled print as «Cancelled» and sends no status when it is not touched', async () => {
    open({ status: 'cancelled', failure_reason: 'userCancelled' });
    expect(statusSelect().value).toBe('cancelled');
    expect(statusSelect().selectedOptions[0].textContent).toBe('Cancelled');
    const body = await save();
    expect(body).not.toHaveProperty('status');
    expect(body).not.toHaveProperty('failure_reason');
  });

  it('clears the recorded reason when a cancelled print becomes completed', async () => {
    open({ status: 'cancelled', failure_reason: 'userCancelled' });
    await userEvent.selectOptions(statusSelect(), 'Completed');
    const body = await save();
    expect(body).toMatchObject({ status: 'completed', failure_reason: null, error_message: null });
  });

  it.each([
    ['stopped', 'Stopped'],
    ['archived', 'Not printed'],
    ['somethingNew', 'somethingNew'],
  ])('shows a stored status it does not offer (%s) as itself', async (status, label) => {
    open({ status });
    expect(statusSelect().value).toBe(status);
    expect(statusSelect().selectedOptions[0].textContent).toBe(label);
    expect(statusSelect().selectedOptions[0]).toBeDisabled();
    expect(statusSelect()).not.toBeDisabled();
    const body = await save();
    expect(body).not.toHaveProperty('status');
  });

  it('locks the status of a running print and says why', async () => {
    open({ status: 'printing' });
    expect(statusSelect()).toBeDisabled();
    expect(statusSelect().selectedOptions[0].textContent).toBe('Printing');
    expect(statusSelect()).toHaveAccessibleDescription(/still running/i);
    const body = await save();
    expect(body).not.toHaveProperty('status');
  });

  it('has no printer control and never sends the printer', async () => {
    open();
    expect(screen.queryByText('Printer')).not.toBeInTheDocument();
    const body = await save();
    expect(body).not.toHaveProperty('printer_id');
  });
});
