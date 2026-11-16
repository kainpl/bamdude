/**
 * The tag filter with a large catalogue.
 *
 * Every user tag used to be a chip in an unbounded wrapping row: a hundred tags
 * were a dozen rows above the file list — and on the widths where the filter
 * panel sticks to the window, a wall that could not be scrolled away. The user
 * tags are now clamped to two rows behind a toggle, with a search box once the
 * catalogue is large. System tags stay outside the clamp and the search.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useNavigate: () => vi.fn() };
});

const stamps = { created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z' };
const SYSTEM = { id: 1, name: '3MF', file_count: 12, is_system: true, code: '3mf', ...stamps };
const userTags = (n: number) =>
  Array.from({ length: n }, (_, i) => ({
    id: 100 + i,
    name: i === 7 ? 'Клієнт Ромашка' : `tag-${i}`,
    file_count: 1,
    is_system: false,
    code: null,
    ...stamps,
  }));

const mockFiles = [
  {
    id: 1,
    filename: 'benchy.gcode.3mf',
    file_path: '/library/benchy.gcode.3mf',
    file_size: 1048576,
    file_type: 'gcode',
    file_tags: ['gcode', '3mf', 'sliced'],
    product_ids: [],
    folder_id: null,
    thumbnail_path: null,
    print_name: 'Benchy',
    print_time_seconds: 3600,
    duplicate_count: 0,
    print_count: 0,
    tags: [],
    created_at: '2024-01-01T00:00:00Z',
  },
];

let tagIdsSeen: string[][] = [];

function mockLibrary(tags: unknown[]) {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json([])),
    http.get('/api/v1/library/files', ({ request }) => {
      tagIdsSeen.push(new URL(request.url).searchParams.getAll('tag_ids'));
      return HttpResponse.json({
        items: mockFiles,
        meta: { total: mockFiles.length, current_page: 1, per_page: 50, last_page: 1 },
      });
    }),
    http.get('/api/v1/library/stats', () =>
      HttpResponse.json({
        total_files: 1,
        total_folders: 0,
        total_size_bytes: 1048576,
        disk_free_bytes: 10737418240,
        disk_total_bytes: 107374182400,
      }),
    ),
    http.get('/api/v1/settings/', () =>
      HttpResponse.json({ check_updates: false, check_printer_firmware: false, library_disk_warning_gb: 5 }),
    ),
    http.get('/api/v1/projects/', () => HttpResponse.json([])),
    http.get('/api/v1/library/tags', () => HttpResponse.json(tags)),
  );
}

/** jsdom lays nothing out: say what the clamped box would measure. */
function stubOverflow(scrollHeight: number, clientHeight: number) {
  const isBox = (el: Element) => (el as HTMLElement).dataset?.testid === 'user-tag-pills';
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return isBox(this) ? scrollHeight : 0;
  });
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return isBox(this) ? clientHeight : 0;
  });
}

const tagRow = async () => (await screen.findByText('Filtering by:')).parentElement!;
const box = async () => within(await tagRow()).getByTestId('user-tag-pills');

describe('the tag filter with a large catalogue', () => {
  beforeEach(() => {
    localStorage.clear();
    tagIdsSeen = [];
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('clamps the user tags to two rows and opens a bounded, scrolling box on demand', async () => {
    mockLibrary([SYSTEM, ...userTags(40)]);
    stubOverflow(400, 52);
    render(<FileManagerPage />);

    const pills = await box();
    expect(pills.className).toContain('max-h-[3.25rem]');
    expect(pills.className).toContain('overflow-hidden');

    const toggle = within(await tagRow()).getByRole('button', { name: 'Show all (40)' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await userEvent.click(toggle);

    const open = await box();
    expect(open.className).not.toContain('max-h-[3.25rem]');
    // Bounded by the window and scrolling inside itself: the panel it sits in
    // is sticky on medium widths, where it gets an even smaller share.
    expect(open.className).toContain('max-h-[40vh]');
    expect(open.className).toContain('sm:max-lg:max-h-[25vh]');
    expect(open.className).toContain('overflow-y-auto');

    await userEvent.click(within(await tagRow()).getByRole('button', { name: 'Show less' }));
    expect((await box()).className).toContain('max-h-[3.25rem]');
  });

  it('offers no toggle when everything fits', async () => {
    mockLibrary([SYSTEM, ...userTags(5)]);
    stubOverflow(22, 22);
    render(<FileManagerPage />);

    await box();
    expect(within(await tagRow()).queryByRole('button', { name: /Show all/ })).not.toBeInTheDocument();
    expect(within(await tagRow()).queryByRole('searchbox')).not.toBeInTheDocument();
  });

  it('keeps the system tags outside the clamp', async () => {
    mockLibrary([SYSTEM, ...userTags(40)]);
    stubOverflow(400, 52);
    render(<FileManagerPage />);

    const pills = await box();
    const system = within(await tagRow()).getByRole('button', { name: '3MF' });
    expect(pills.contains(system)).toBe(false);
  });

  it('searches the user tags from thirteen up, whatever the case or alphabet — never the system tags', async () => {
    mockLibrary([SYSTEM, ...userTags(13)]);
    render(<FileManagerPage />);

    const search = within(await tagRow()).getByRole('searchbox', { name: 'Search tags' });
    await userEvent.type(search, 'РОМАШ');

    const row = await tagRow();
    expect(within(row).getByRole('button', { name: 'Клієнт Ромашка' })).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: 'tag-3' })).not.toBeInTheDocument();
    expect(within(row).getByRole('button', { name: '3MF' })).toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, 'zzz');
    expect(within(await tagRow()).getByText('No tags match')).toBeInTheDocument();
  });

  it('has no search box at twelve tags', async () => {
    mockLibrary([SYSTEM, ...userTags(12)]);
    render(<FileManagerPage />);

    await box();
    expect(within(await tagRow()).queryByRole('searchbox')).not.toBeInTheDocument();
  });

  it('puts a selected tag first and keeps it through a search that does not match it', async () => {
    mockLibrary([SYSTEM, ...userTags(20)]);
    render(<FileManagerPage />);

    await userEvent.click(within(await box()).getByRole('button', { name: 'tag-15' }));
    await waitFor(() => expect(tagIdsSeen.at(-1)).toEqual(['115']));

    await waitFor(async () =>
      expect(within(await box()).getAllByRole('button')[0]).toHaveAccessibleName('tag-15'),
    );

    await userEvent.type(within(await tagRow()).getByRole('searchbox'), 'tag-3');
    const pills = await box();
    expect(within(pills).getByRole('button', { name: 'tag-15' })).toBeInTheDocument();
    expect(within(pills).getByRole('button', { name: 'tag-3' })).toBeInTheDocument();
    expect(within(pills).queryByRole('button', { name: 'tag-4' })).not.toBeInTheDocument();
  });

  it('opens the box when the keyboard focus lands on a chip the clamp hides', async () => {
    mockLibrary([SYSTEM, ...userTags(40)]);
    stubOverflow(400, 52);
    render(<FileManagerPage />);

    const pills = await box();
    const hidden = within(pills).getByRole('button', { name: 'tag-39' });
    vi.spyOn(pills, 'getBoundingClientRect').mockReturnValue({ bottom: 52 } as DOMRect);
    vi.spyOn(hidden, 'getBoundingClientRect').mockReturnValue({ bottom: 300 } as DOMRect);
    hidden.focus();

    await waitFor(async () => expect((await box()).className).toContain('overflow-y-auto'));
  });
});
