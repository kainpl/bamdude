/**
 * «Result not checked yet» on the archive page (upstream audit G04, part C).
 *
 * - The server says which prints are unchecked; the page only shows the badge.
 * - «Unchecked» is a server-side filter whose state is the URL, so the
 *   reminder's link opens exactly that list.
 * - «No defects» marks the result as checked through its own route — it sends
 *   no defect count — and asks the archive's update right, as Edit does.
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

const archive = (id: number, name: string, extra: Record<string, unknown> = {}) => ({
  id,
  filename: `${name}.gcode.3mf`,
  file_path: `archive/1/${name}.gcode.3mf`,
  print_name: name,
  printer_id: null,
  status: 'completed',
  created_at: '2026-10-01T09:00:00Z',
  project_id: null,
  project_name: null,
  created_by_id: 5,
  tags: '',
  defective_count: 0,
  outcome_unchecked: false,
  outcome_checked_at: '2026-10-01T10:00:00Z',
  ...extra,
});

const ARCHIVES = [
  archive(1, 'Fresh', { outcome_unchecked: true, outcome_checked_at: null }),
  archive(2, 'Checked'),
  archive(3, 'Failed', { status: 'failed' }),
  archive(4, 'Theirs', { outcome_unchecked: true, outcome_checked_at: null, created_by_id: 8 }),
];

const listRequests: URLSearchParams[] = [];
const marked: number[] = [];

function cardOf(id: number): HTMLElement {
  return document.querySelector(`[data-archive-id="${id}"]`) as HTMLElement;
}

async function openMenu(id: number): Promise<void> {
  fireEvent.click(within(cardOf(id)).getByTitle('Right-click for more options'));
  await screen.findAllByRole('button', { name: 'Edit' });
}

function menuButton(name: string): HTMLElement | undefined {
  return screen.queryAllByRole('button', { name }).find((el) => el.closest('.fixed')) as HTMLElement | undefined;
}

describe('ArchivesPage — result not checked', () => {
  beforeEach(() => {
    localStorage.clear();
    listRequests.length = 0;
    marked.length = 0;
    window.history.pushState({}, '', '/archives');
    auth.granted = new Set(['archives:read_all', 'archives:read', 'archives:update_own']);
    server.use(
      http.get('/api/v1/archives/', ({ request }) => {
        const params = new URL(request.url).searchParams;
        listRequests.push(params);
        const data = params.get('outcome') === 'unchecked' ? ARCHIVES.filter((a) => a.outcome_unchecked) : ARCHIVES;
        return HttpResponse.json({ data, meta: { current_page: 1, per_page: 50, total: data.length, last_page: 1 } });
      }),
      http.post('/api/v1/archives/:id/outcome-checked', ({ params }) => {
        marked.push(Number(params.id));
        return HttpResponse.json(archive(Number(params.id), 'Fresh'));
      }),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/archives/tags', () => HttpResponse.json([])),
    );
  });

  it('badges only the prints the server calls unchecked', async () => {
    render(<ArchivesPage />);
    await waitFor(() => expect(cardOf(1)).not.toBeNull());

    expect(within(cardOf(1)).getByTestId('archive-outcome-unchecked')).toHaveTextContent('Not checked');
    expect(within(cardOf(2)).queryByTestId('archive-outcome-unchecked')).toBeNull();
    expect(within(cardOf(3)).queryByTestId('archive-outcome-unchecked')).toBeNull();
  });

  it('«Unchecked» asks the server for that list and keeps its state in the URL', async () => {
    render(<ArchivesPage />);
    await waitFor(() => expect(cardOf(2)).not.toBeNull());
    expect(listRequests.at(-1)?.get('outcome')).toBeNull();

    const toggle = screen.getByRole('button', { name: /unchecked/i });
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(toggle);

    await waitFor(() => expect(listRequests.at(-1)?.get('outcome')).toBe('unchecked'));
    expect(window.location.search).toContain('outcome=unchecked');
    await waitFor(() => expect(cardOf(2)).toBeNull());
    expect(screen.getByRole('button', { name: /unchecked/i })).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(screen.getByRole('button', { name: /unchecked/i }));
    await waitFor(() => expect(listRequests.at(-1)?.get('outcome')).toBeNull());
    expect(window.location.search).not.toContain('outcome');
  });

  it('opens already filtered from the reminder’s link', async () => {
    window.history.pushState({}, '', '/archives?outcome=unchecked');
    render(<ArchivesPage />);

    await waitFor(() => expect(cardOf(1)).not.toBeNull());
    expect(listRequests.every((params) => params.get('outcome') === 'unchecked')).toBe(true);
    expect(cardOf(2)).toBeNull();
    expect(marked).toEqual([]);
  });

  it('«No defects» marks the print through its own route', async () => {
    render(<ArchivesPage />);
    await waitFor(() => expect(cardOf(1)).not.toBeNull());

    await openMenu(1);
    const item = menuButton('No defects');
    expect(item).toBeDefined();
    expect(item).not.toBeDisabled();
    fireEvent.click(item as HTMLElement);

    await waitFor(() => expect(marked).toEqual([1]));
  });

  it('is offered only for an unchecked print, and only to who may edit it', async () => {
    render(<ArchivesPage />);
    await waitFor(() => expect(cardOf(4)).not.toBeNull());

    await openMenu(2);
    expect(menuButton('No defects')).toBeUndefined();
    fireEvent.keyDown(document, { key: 'Escape' });

    await openMenu(4);
    const theirs = menuButton('No defects');
    expect(theirs).toBeDisabled();
    expect(theirs).toHaveAttribute('title', 'You do not have permission to update archives');
    expect(marked).toEqual([]);
  });
});
