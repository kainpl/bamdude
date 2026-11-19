/**
 * The archive page's doors to NEW print work (upstream audit G04).
 *
 * - Print and Schedule open the dialog on the plate the archive PRINTED
 *   (`plate_index`), not the first plate of its file.
 * - Scheduling an archive is a reprint of it: beside `queue:create` it needs the
 *   reprint right for that archive, as the server asks.
 * - A print that did not finish and was never measured says its cost is one
 *   full print's.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ArchivesPage } from '../../pages/ArchivesPage';
import type { Permission } from '../../api/client';

const auth = vi.hoisted(() => ({ granted: new Set<string>(), userId: 5 }));

vi.mock('../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({
      ...actual.useAuth(),
      hasPermission: (p: Permission) => auth.granted.has(p),
      hasAnyPermission: (...ps: Permission[]) => ps.some((p) => auth.granted.has(p)),
      canModify: (resource: string, action: string, createdById: number | null | undefined) => {
        if (auth.granted.has(`${resource}:${action}_all`)) return true;
        if (auth.granted.has(`${resource}:${action}_own`)) return createdById != null && createdById === auth.userId;
        return false;
      },
    }),
  };
});

// The dialog itself is covered by its own tests; here only what the page hands it.
vi.mock('../../components/PrintModal', () => ({
  PrintModal: (props: { mode: string; archiveId?: number; preselectedPlateId?: number | null }) => (
    <div
      data-testid="print-modal"
      data-mode={props.mode}
      data-archive={String(props.archiveId)}
      data-plate={String(props.preselectedPlateId)}
    />
  ),
}));

const archive = (id: number, name: string, createdById: number | null, extra: Record<string, unknown> = {}) => ({
  id,
  filename: `${name}.gcode.3mf`,
  file_path: `archive/1/${name}.gcode.3mf`,
  print_name: name,
  printer_id: null,
  status: 'completed',
  created_at: '2026-10-01T09:00:00Z',
  project_id: null,
  project_name: null,
  created_by_id: createdById,
  tags: '',
  ...extra,
});

const ARCHIVES = [
  archive(1, 'Mine', 5, { plate_index: 3 }),
  archive(2, 'Theirs', 8, { plate_index: 2 }),
  archive(3, 'Unmeasured', 5, { status: 'failed', cost: 4.5, cost_is_full_print: true }),
  archive(4, 'Measured', 5, { status: 'failed', cost: 1.25, cost_is_full_print: false }),
];

function cardOf(id: number): HTMLElement {
  return document.querySelector(`[data-archive-id="${id}"]`) as HTMLElement;
}

async function menuItem(id: number, name: string): Promise<HTMLElement> {
  fireEvent.click(within(cardOf(id)).getByTitle('Right-click for more options'));
  // The card carries buttons of the same names; the menu's own sit in its fixed box.
  const found = await screen.findAllByRole('button', { name });
  return found.find((el) => el.closest('.fixed')) as HTMLElement;
}

describe('ArchivesPage — print work from an archive', () => {
  beforeEach(() => {
    localStorage.clear();
    auth.granted = new Set(['archives:read_all', 'archives:read', 'queue:create']);
    server.use(
      http.get('/api/v1/archives/', () =>
        HttpResponse.json({ data: ARCHIVES, meta: { current_page: 1, per_page: 50, total: 4, last_page: 1 } }),
      ),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/archives/tags', () => HttpResponse.json([])),
    );
  });

  it.each([
    ['Print', 'reprint'],
    ['Schedule', 'add-to-queue'],
  ])('«%s» opens on the plate the archive printed', async (label, mode) => {
    auth.granted = new Set([...auth.granted, 'archives:reprint_all']);
    render(<ArchivesPage />);
    await waitFor(() => expect(cardOf(1)).not.toBeNull());

    fireEvent.click(await menuItem(1, label));

    const modal = await screen.findByTestId('print-modal');
    expect(modal).toHaveAttribute('data-mode', mode);
    expect(modal).toHaveAttribute('data-archive', '1');
    expect(modal).toHaveAttribute('data-plate', '3');
  });

  it('scheduling someone else’s print needs more than «reprint own»', async () => {
    auth.granted = new Set([...auth.granted, 'archives:reprint_own']);
    render(<ArchivesPage />);
    await waitFor(() => expect(cardOf(2)).not.toBeNull());

    expect(await menuItem(1, 'Schedule')).not.toBeDisabled();
    fireEvent.keyDown(document, { key: 'Escape' });
    const theirs = await menuItem(2, 'Schedule');
    expect(theirs).toBeDisabled();
    expect(theirs).toHaveAttribute('title', 'You do not have permission to reprint this archive');
  });

  it('scheduling needs the reprint right at all, beside the queue right', async () => {
    render(<ArchivesPage />);
    await waitFor(() => expect(cardOf(1)).not.toBeNull());

    const item = await menuItem(1, 'Schedule');
    expect(item).toBeDisabled();
    expect(item).toHaveAttribute('title', 'You do not have permission to reprint this archive');
  });

  it('an unmeasured failed print says its cost is one full print’s', async () => {
    render(<ArchivesPage />);
    await waitFor(() => expect(cardOf(4)).not.toBeNull());

    const hint = /cost of one full print/i;
    expect(within(cardOf(3)).getByTitle(hint)).toHaveTextContent('4.50');
    expect(within(cardOf(4)).queryByTitle(hint)).toBeNull();
    expect(within(cardOf(4)).getByText(/1\.25/)).toBeInTheDocument();
  });
});
