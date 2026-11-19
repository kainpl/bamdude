/**
 * The archive editor and «result not checked» (upstream audit G04, part C).
 *
 * Every defects write marks the result as checked on the server, so the editor
 * must not send a defect count it was not asked to change — saving a note is
 * not grading the plate. «No defects» is the explicit way to confirm a zero.
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
  quantity: 3,
  defective_count: 0,
  parts: [],
  outcome_unchecked: true,
  outcome_checked_at: null,
  created_at: '2026-11-01T00:00:00Z',
  created_by_id: null,
} as unknown as Archive;

const patched: Record<string, unknown>[] = [];
const marked: number[] = [];

function open(overrides: Partial<Archive> = {}) {
  return render(<EditArchiveModal archive={{ ...base, ...overrides } as Archive} onClose={vi.fn()} existingTags={['x']} />);
}

describe('EditArchiveModal — result not checked', () => {
  beforeEach(() => {
    patched.length = 0;
    marked.length = 0;
    server.use(
      http.get('/api/v1/projects/', () =>
        HttpResponse.json({ items: [], meta: { total: 0, current_page: 1, per_page: 20, last_page: 1 } }),
      ),
      http.patch('/api/v1/archives/:id', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patched.push(body);
        return HttpResponse.json({ ...base, ...body });
      }),
      http.post('/api/v1/archives/:id/outcome-checked', ({ params }) => {
        marked.push(Number(params.id));
        return HttpResponse.json({ ...base, outcome_unchecked: false, outcome_checked_at: '2026-11-01T01:00:00Z' });
      }),
    );
  });

  it('saving without touching the defect count sends none', async () => {
    open();
    await userEvent.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => expect(patched).toHaveLength(1));
    expect(patched[0]).not.toHaveProperty('defective_count');
    expect(patched[0]).not.toHaveProperty('parts_defective');
  });

  it('«No defects» marks the result through its own route and sends no count', async () => {
    open();
    await userEvent.click(screen.getByRole('button', { name: 'No defects' }));

    await waitFor(() => expect(marked).toEqual([7]));
    expect(patched).toEqual([]);
    await waitFor(() => expect(screen.queryByRole('button', { name: 'No defects' })).toBeNull());
  });

  it('a print already checked is not asked again', () => {
    open({ outcome_unchecked: false, outcome_checked_at: '2026-11-01T01:00:00Z' } as Partial<Archive>);
    expect(screen.queryByRole('button', { name: 'No defects' })).toBeNull();
  });
});
