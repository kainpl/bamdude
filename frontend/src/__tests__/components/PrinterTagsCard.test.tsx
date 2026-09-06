import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useQuery } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrinterTagsCard } from '../../components/settings/PrinterTagsCard';

const TAGS = [
  { id: 1, name: 'Фаза 1', color: null, printer_count: 2, is_stagger_group: true },
  { id: 2, name: 'Замовник X', color: null, printer_count: 0, is_stagger_group: false },
];

/**
 * The row a tag owns.
 *
 * ⚠️ Never `getAllByTitle('Delete')[0]` — the card sorts by name the way a
 * Ukrainian reader does, so З comes before Ф and the first row on screen is
 * NOT the first entry in the fixture. Reaching for a row by index is how a
 * test quietly asserts about a different tag than the one it names.
 */
function rowOf(name: string): HTMLElement {
  const row = screen.getByText(name).closest('li');
  if (!row) throw new Error(`no row for ${name}`);
  return row as HTMLElement;
}

/**
 * A real `['printers']` consumer, mounted beside the card.
 *
 * The printer cards carry the resolved tag NAMES, so a rename that does not
 * reach their cache leaves the old label on screen until something else
 * happens to refetch. Asserting that this probe refetches is the effect that
 * matters; spying on `invalidateQueries` would only assert the call.
 */
function PrintersProbe({ onFetch }: { onFetch: () => void }) {
  useQuery({
    queryKey: ['printers'],
    queryFn: async () => {
      onFetch();
      return [];
    },
  });
  return null;
}

describe('PrinterTagsCard', () => {
  // The delete button asks `window.confirm` for a worn tag, and jsdom's own
  // `confirm` is a not-implemented stub that answers undefined — i.e. "no".
  // Every test that expects a DELETE to reach the server has to answer it.
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('lists tags with counts and badges a stagger group', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: TAGS })));
    render(<PrinterTagsCard />);
    expect(await screen.findByText('Фаза 1')).toBeInTheDocument();
    expect(screen.getByText('stagger group')).toBeInTheDocument();
    expect(screen.getByText('2 printers')).toBeInTheDocument();
    // Only the stagger group is badged.
    expect(within(rowOf('Замовник X')).queryByText('stagger group')).not.toBeInTheDocument();
  });

  it('creates a tag and refreshes the list', async () => {
    let created = false;
    server.use(
      http.get('/api/v1/printer-tags', () =>
        HttpResponse.json({
          tags: created ? [...TAGS, { id: 3, name: 'Ряд 2', printer_count: 0, is_stagger_group: false }] : TAGS,
        })
      ),
      http.post('/api/v1/printer-tags', async () => {
        created = true;
        return HttpResponse.json({ id: 3, name: 'Ряд 2' }, { status: 201 });
      })
    );
    render(<PrinterTagsCard />);
    await screen.findByText('Фаза 1');
    await userEvent.type(screen.getByPlaceholderText('Add tag'), 'Ряд 2');
    await userEvent.click(screen.getByRole('button', { name: 'Add tag' }));
    expect(await screen.findByText('Ряд 2')).toBeInTheDocument();
  });

  it('renames a tag in place', async () => {
    let renamed = false;
    const patched: string[] = [];
    server.use(
      http.get('/api/v1/printer-tags', () =>
        HttpResponse.json({ tags: renamed ? [{ ...TAGS[0], name: 'Фаза A' }, TAGS[1]] : TAGS })
      ),
      http.patch('/api/v1/printer-tags/1', async ({ request }) => {
        const body = (await request.json()) as { name: string };
        patched.push(body.name);
        renamed = true;
        return HttpResponse.json({ id: 1, name: body.name });
      })
    );
    render(<PrinterTagsCard />);
    await screen.findByText('Фаза 1');
    await userEvent.click(within(rowOf('Фаза 1')).getByTitle('Edit'));
    const field = await screen.findByLabelText('Edit Фаза 1');
    await userEvent.clear(field);
    await userEvent.type(field, 'Фаза A{Enter}');
    expect(await screen.findByText('Фаза A')).toBeInTheDocument();
    expect(patched).toEqual(['Фаза A']);
  });

  it('leaves a rename field without writing when the name did not change', async () => {
    let patchCalls = 0;
    server.use(
      http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: TAGS })),
      http.patch('/api/v1/printer-tags/1', () => {
        patchCalls += 1;
        return HttpResponse.json({ id: 1, name: 'Фаза 1' });
      })
    );
    render(<PrinterTagsCard />);
    await screen.findByText('Фаза 1');
    await userEvent.click(within(rowOf('Фаза 1')).getByTitle('Edit'));
    await screen.findByLabelText('Edit Фаза 1');
    // Blur without touching anything — this is how you leave a row you opened
    // by mistake, and it must not become a PATCH nobody asked for.
    await userEvent.click(screen.getByPlaceholderText('Add tag'));
    await waitFor(() => expect(screen.queryByLabelText('Edit Фаза 1')).not.toBeInTheDocument());
    expect(patchCalls).toBe(0);
  });

  /**
   * ⚠️ The recolour PATCH carries the CURRENT name beside the colour.
   *
   * The backend's patch schema inherits the create one, so `name` is required —
   * a body of `{color}` alone is a 422. Sending the name the row already has is
   * what keeps that contract untouched while a colour changes.
   */
  it('picks a colour from the palette and sends it', async () => {
    const patched: unknown[] = [];
    server.use(
      http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: TAGS })),
      http.patch('/api/v1/printer-tags/1', async ({ request }) => {
        patched.push(await request.json());
        return HttpResponse.json({ id: 1, name: 'Фаза 1', color: '#f59e0b' });
      })
    );
    render(<PrinterTagsCard />);
    await screen.findByText('Фаза 1');
    await userEvent.click(screen.getByRole('button', { name: 'Colour of Фаза 1' }));
    await userEvent.click(screen.getByRole('button', { name: 'Amber' }));
    await waitFor(() => expect(patched).toEqual([{ name: 'Фаза 1', color: '#f59e0b' }]));
  });

  /** "No colour" is an explicit null — an empty string is a 422 at the backend. */
  it('clears the colour', async () => {
    const patched: unknown[] = [];
    server.use(
      http.get('/api/v1/printer-tags', () =>
        HttpResponse.json({ tags: [{ ...TAGS[0], color: '#f59e0b' }, TAGS[1]] })
      ),
      http.patch('/api/v1/printer-tags/1', async ({ request }) => {
        patched.push(await request.json());
        return HttpResponse.json({ id: 1, name: 'Фаза 1', color: null });
      })
    );
    render(<PrinterTagsCard />);
    await screen.findByText('Фаза 1');
    await userEvent.click(screen.getByRole('button', { name: 'Colour of Фаза 1' }));
    await userEvent.click(screen.getByRole('button', { name: 'No colour' }));
    await waitFor(() => expect(patched).toEqual([{ name: 'Фаза 1', color: null }]));
  });

  it('shows the backend sentence when a stagger group cannot be deleted', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true); // Фаза 1 is worn by two printers
    server.use(
      http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: TAGS })),
      http.delete('/api/v1/printer-tags/1', () =>
        HttpResponse.json(
          {
            detail:
              'This tag is a staggered-start group. Un-choose it under Settings → Printing → Queue & Scheduling → Staggered start first.',
          },
          { status: 409 }
        )
      )
    );
    render(<PrinterTagsCard />);
    await screen.findByText('Фаза 1');
    await userEvent.click(within(rowOf('Фаза 1')).getByTitle('Delete'));
    await waitFor(() => expect(screen.getByText(/staggered-start group/)).toBeInTheDocument());
  });

  it('refreshes the printers cache after a rename, so no card keeps the old label', async () => {
    server.use(
      http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: TAGS })),
      http.patch('/api/v1/printer-tags/1', () => HttpResponse.json({ id: 1, name: 'Фаза A' }))
    );
    const onFetch = vi.fn();
    render(
      <>
        <PrintersProbe onFetch={onFetch} />
        <PrinterTagsCard />
      </>
    );
    await screen.findByText('Фаза 1');
    await waitFor(() => expect(onFetch).toHaveBeenCalledTimes(1));

    await userEvent.click(within(rowOf('Фаза 1')).getByTitle('Edit'));
    const field = await screen.findByLabelText('Edit Фаза 1');
    await userEvent.clear(field);
    await userEvent.type(field, 'Фаза A{Enter}');

    await waitFor(() => expect(onFetch).toHaveBeenCalledTimes(2));
  });

  it('drops the row once a delete succeeds', async () => {
    let deleted = false;
    server.use(
      http.get('/api/v1/printer-tags', () =>
        HttpResponse.json({ tags: deleted ? TAGS.filter((tag) => tag.id !== 2) : TAGS })
      ),
      http.delete('/api/v1/printer-tags/2', () => {
        deleted = true;
        return HttpResponse.json({ deleted: 1 });
      })
    );
    render(<PrinterTagsCard />);
    await screen.findByText('Замовник X');
    await userEvent.click(within(rowOf('Замовник X')).getByTitle('Delete'));

    await waitFor(() => expect(screen.queryByText('Замовник X')).not.toBeInTheDocument());
    // The refusal line belongs to a refusal — a success must not print one.
    expect(screen.queryByText(/already exists|staggered-start/)).not.toBeInTheDocument();
    expect(screen.getByText('Фаза 1')).toBeInTheDocument();
  });

  it('asks before removing a tag printers wear, and does nothing when refused', async () => {
    let deleteCalls = 0;
    server.use(
      http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: TAGS })),
      http.delete('/api/v1/printer-tags/1', () => {
        deleteCalls += 1;
        return HttpResponse.json({ deleted: 1 });
      })
    );
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<PrinterTagsCard />);
    await screen.findByText('Фаза 1');
    await userEvent.click(within(rowOf('Фаза 1')).getByTitle('Delete'));

    expect(confirmSpy).toHaveBeenCalledWith('Remove the tag "Фаза 1" from 2 printers?');
    // Not merely "no row disappeared" — the request must never have been sent.
    expect(deleteCalls).toBe(0);
    expect(screen.getByText('Фаза 1')).toBeInTheDocument();
  });

  it('removes the tag once the operator confirms', async () => {
    let deleted = false;
    server.use(
      http.get('/api/v1/printer-tags', () =>
        HttpResponse.json({ tags: deleted ? TAGS.filter((tag) => tag.id !== 1) : TAGS })
      ),
      http.delete('/api/v1/printer-tags/1', () => {
        deleted = true;
        return HttpResponse.json({ deleted: 1 });
      })
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<PrinterTagsCard />);
    await screen.findByText('Фаза 1');
    await userEvent.click(within(rowOf('Фаза 1')).getByTitle('Delete'));

    await waitFor(() => expect(screen.queryByText('Фаза 1')).not.toBeInTheDocument());
  });

  it('deletes a tag nobody wears without asking', async () => {
    let deleted = false;
    server.use(
      http.get('/api/v1/printer-tags', () =>
        HttpResponse.json({ tags: deleted ? TAGS.filter((tag) => tag.id !== 2) : TAGS })
      ),
      http.delete('/api/v1/printer-tags/2', () => {
        deleted = true;
        return HttpResponse.json({ deleted: 1 });
      })
    );
    // Answering "no" proves the prompt is never reached: the row goes anyway.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<PrinterTagsCard />);
    await screen.findByText('Замовник X');
    await userEvent.click(within(rowOf('Замовник X')).getByTitle('Delete'));

    await waitFor(() => expect(screen.queryByText('Замовник X')).not.toBeInTheDocument());
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('counts one printer in the singular', async () => {
    server.use(
      http.get('/api/v1/printer-tags', () =>
        HttpResponse.json({ tags: [{ id: 7, name: 'Ряд 1', printer_count: 1, is_stagger_group: false }] })
      )
    );
    render(<PrinterTagsCard />);
    expect(await screen.findByText('1 printer')).toBeInTheDocument();
  });

  it('invites the operator to add one when there are none', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: [] })));
    render(<PrinterTagsCard />);
    expect(await screen.findByText(/No tags yet/)).toBeInTheDocument();
  });
});
