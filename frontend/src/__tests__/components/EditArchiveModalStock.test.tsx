/**
 * «Count into stock» on the archive editor (pass 8, Decision 3).
 *
 * New order-less prints are credited automatically by the completion handler;
 * HISTORY deliberately is not, because nobody knows which of last year's
 * order-less prints were shipped, scrapped or are still in a drawer. This
 * button is the operator vouching for one of them, so the two things it must
 * never get wrong are WHEN it is offered — an archive filed under an order has
 * its parts counted there already — and what it says when the server refuses.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { api, ApiError } from '../../api/client';
import type { Archive, Permission } from '../../api/client';
import { EditArchiveModal } from '../../components/EditArchiveModal';
import { server } from '../mocks/server';

const auth = vi.hoisted(() => ({ granted: null as Set<string> | null }));

vi.mock('../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => {
      const real = actual.useAuth();
      return { ...real, hasPermission: (p: Permission) => auth.granted?.has(p) ?? true };
    },
  };
});

// Deliberately partial: the modal reads a dozen of `Archive`'s sixty fields and
// spelling out the rest would bury the two this file is about.
const archive = {
  id: 7,
  printer_id: 1,
  project_id: null,
  project_line_id: null,
  print_name: 'Benchy',
  filename: 'benchy.gcode.3mf',
  status: 'completed',
  quantity: 1,
  defective_count: 0,
  tags: '',
  notes: '',
  photos: null,
  parts: [{ id: 1, name: 'lid', name_key: 'lid', quantity: 2, defective: 0 }],
} as unknown as Archive;

describe('EditArchiveModal · count into stock', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    auth.granted = null;
    server.use(
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/archives/tags', () => HttpResponse.json([])),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
    );
  });

  it('is offered for a print filed under no order', async () => {
    render(<EditArchiveModal archive={archive} onClose={vi.fn()} />);

    expect(await screen.findByTestId('archive-count-into-stock')).toBeInTheDocument();
  });

  it('is not offered for a print that already belongs to an order', () => {
    // Its parts are counted against that order's lines — putting them on a
    // shelf as well would count them twice, which is what the endpoint 409s.
    render(<EditArchiveModal archive={{ ...archive, project_id: 3 }} onClose={vi.fn()} />);

    expect(screen.queryByTestId('archive-count-into-stock')).not.toBeInTheDocument();
  });

  it('is not offered to a reader', () => {
    auth.granted = new Set(['archives:read']);
    render(<EditArchiveModal archive={archive} onClose={vi.fn()} />);

    expect(screen.queryByTestId('archive-count-into-stock')).not.toBeInTheDocument();
  });

  it('posts and says what landed on the shelf', async () => {
    const count = vi
      .spyOn(api, 'countArchiveIntoStock')
      .mockResolvedValue([{ part_id: 1, name: 'lids', delta: 2 }]);

    render(<EditArchiveModal archive={archive} onClose={vi.fn()} />);
    fireEvent.click(await screen.findByTestId('archive-count-into-stock'));

    await waitFor(() => expect(count).toHaveBeenCalledWith(7));
    expect(await screen.findByText('2 lids → free stock')).toBeInTheDocument();
  });

  it('says plainly when the print counted nothing', async () => {
    // ⚠️ An empty list is a legitimate answer, not a failure: the print may
    // have finished nothing good, or its plate may belong to no product. A
    // green toast listing nothing would read as a success nobody can verify.
    vi.spyOn(api, 'countArchiveIntoStock').mockResolvedValue([]);

    render(<EditArchiveModal archive={archive} onClose={vi.fn()} />);
    fireEvent.click(await screen.findByTestId('archive-count-into-stock'));

    expect(await screen.findByText(/counted nothing into stock/i)).toBeInTheDocument();
  });

  it('reports a refusal in the server own words', async () => {
    vi.spyOn(api, 'countArchiveIntoStock').mockRejectedValue(
      new ApiError('This print has already been counted into stock', 409),
    );

    render(<EditArchiveModal archive={archive} onClose={vi.fn()} />);
    fireEvent.click(await screen.findByTestId('archive-count-into-stock'));

    expect(await screen.findByText('This print has already been counted into stock')).toBeInTheDocument();
  });
});
