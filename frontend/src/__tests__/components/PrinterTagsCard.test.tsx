import { describe, it, expect } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrinterTagsCard } from '../../components/settings/PrinterTagsCard';

const TAGS = [
  { id: 1, name: 'Фаза 1', printer_count: 2, is_stagger_group: true },
  { id: 2, name: 'Замовник X', printer_count: 0, is_stagger_group: false },
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

describe('PrinterTagsCard', () => {
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

  it('shows the backend sentence when a stagger group cannot be deleted', async () => {
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

  it('invites the operator to add one when there are none', async () => {
    server.use(http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: [] })));
    render(<PrinterTagsCard />);
    expect(await screen.findByText(/No tags yet/)).toBeInTheDocument();
  });
});
