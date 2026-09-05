/**
 * Re-reading the card is always something somebody asked for, from a NAMED
 * file: linking a file does not fill anything on its own, and a product with
 * several linked files has no obvious "the" file to read. Hence a picker, not
 * a single button.
 *
 * The notes come back as CODES with params (`CardNote`) — the server has no
 * idea which language the operator reads — so the phrasing is asserted here in
 * English, which is where it lives.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { QueryClient } from '@tanstack/react-query';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { api, ApiError } from '../../../api/client';
import type { Product } from '../../../api/client';
import { ProductHeader } from '../../../components/products/ProductHeader';

const product = {
  id: 7,
  name: 'Flask',
  is_active: true,
  has_cover: false,
  cover_image_filename: null,
  description: null,
  notes: null,
  designer: null,
  license: null,
  source_url: null,
  design_id: null,
  attachments: [],
  parts: [],
  library_file_ids: [3],
  library_folder_ids: [],
  units_printed_total: 0,
} as unknown as Product;

const noop = () => {};

function mount(over: Partial<Product> = {}) {
  render(
    <ProductHeader
      product={{ ...product, ...over }}
      onEdit={noop}
      onDuplicate={noop}
      onDelete={noop}
      onToggleActive={noop}
    />,
  );
}

describe('ProductHeader — re-read from file', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([{ id: 3, filename: 'flask.3mf' }] as never);
  });

  it('re-reads the card from the file the operator picked and reports what it did', async () => {
    const reread = vi.spyOn(api, 'rereadProductCard').mockResolvedValue({
      product,
      notes: [
        { code: 'filled_field', params: { field: 'designer' } },
        { code: 'imported_files', params: { category: 'pictures', count: 2 } },
      ],
    } as never);
    mount();

    // `findBy`, not `getBy`: the render helper's AuthProvider loads the user
    // asynchronously, and until it has, `hasPermission` is false and every
    // edit control — this button included — is still absent.
    fireEvent.click(await screen.findByRole('button', { name: /re-read/i }));
    fireEvent.click(await screen.findByRole('menuitem', { name: /flask\.3mf/i }));

    await waitFor(() => expect(reread).toHaveBeenCalledWith(7, 3));
    expect(await screen.findByText(/filled in designer/i)).toBeInTheDocument();
    expect(screen.getByText(/imported 2 files into pictures/i)).toBeInTheDocument();
  });

  it('says there was nothing to fill rather than nothing at all', async () => {
    vi.spyOn(api, 'rereadProductCard').mockResolvedValue({
      product,
      notes: [{ code: 'nothing_to_fill', params: {} }],
    } as never);
    mount();

    // `findBy`, not `getBy`: the render helper's AuthProvider loads the user
    // asynchronously, and until it has, `hasPermission` is false and every
    // edit control — this button included — is still absent.
    fireEvent.click(await screen.findByRole('button', { name: /re-read/i }));
    fireEvent.click(await screen.findByRole('menuitem', { name: /flask\.3mf/i }));

    expect(await screen.findByText(/nothing to fill/i)).toBeInTheDocument();
  });

  it('says what to link when the product has no file to re-read from', async () => {
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([] as never);
    mount({ library_file_ids: [] });

    // `findBy`, not `getBy`: the render helper's AuthProvider loads the user
    // asynchronously, and until it has, `hasPermission` is false and every
    // edit control — this button included — is still absent.
    fireEvent.click(await screen.findByRole('button', { name: /re-read/i }));
    // A disabled `menuitem`, not a `<p>`: a menu whose only row is not a row is
    // a menu a screen reader reads as empty, and "there is nothing to re-read
    // from" is the answer the operator opened it for.
    const empty = await screen.findByRole('menuitem', { name: /link a file/i });
    expect(empty).toBeDisabled();
  });

  it('refreshes the order cards too — a re-read can give the product its first cover', async () => {
    // The 3MF's Model Pictures land as attachments, and the FIRST picture is
    // the implicit cover. An order card renders that cover off the `projects`
    // query, so without this invalidation the card keeps its placeholder until
    // something unrelated happens to refetch orders.
    vi.spyOn(api, 'rereadProductCard').mockResolvedValue({ product, notes: [] } as never);
    const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries');
    mount();

    fireEvent.click(await screen.findByRole('button', { name: /re-read/i }));
    fireEvent.click(await screen.findByRole('menuitem', { name: /flask\.3mf/i }));

    // ⚠️ The PREFIX, not `['product', 7]`: since Ruling 29 the product keys are
    // order views, so one `invalidateOrderViews` covers them — naming them
    // again here was two refetches of the same page for one re-read.
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['product'] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['products'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['projects'] });
  });

  it('names the picker as a menu before it is opened', async () => {
    // The popup carries `role="menu"`, but a screen reader meets the TRIGGER
    // first; without these it is announced as an ordinary button and nothing
    // says a menu opens, or that one is open.
    mount();

    const trigger = await screen.findByRole('button', { name: /re-read/i });
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
  });

  it('closes the picker on Escape — a click is not the only way out', async () => {
    mount();

    fireEvent.click(await screen.findByRole('button', { name: /re-read/i }));
    expect(await screen.findByRole('menu')).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('hides the picker from somebody who may not change the product', async () => {
    // Gating is a claim, and a claim has to be tested: the re-read WRITES the
    // product's card, so a viewer must not be offered it.
    server.use(
      http.get('/api/v1/auth/me', () =>
        HttpResponse.json({
          id: 2,
          username: 'viewer',
          role: 'user',
          is_active: true,
          is_admin: false,
          groups: [{ id: 2, name: 'Viewers' }],
          permissions: ['projects:read'],
          created_at: '2024-01-01T00:00:00Z',
        }),
      ),
    );
    const me = vi.spyOn(api, 'getCurrentUser');
    mount();

    await waitFor(() => expect(me).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: /re-read/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^edit$/i })).not.toBeInTheDocument();
  });
});

describe('ProductHeader — export', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([] as never);
  });

  it('downloads the product as a ZIP', async () => {
    const save = vi.spyOn(api, 'downloadProductExport').mockResolvedValue(undefined);
    mount();

    fireEvent.click(await screen.findByRole('button', { name: /export/i }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(7));
  });

  it('says the export failed instead of leaving the button spinning', async () => {
    vi.spyOn(api, 'downloadProductExport').mockRejectedValue(new ApiError('nope', 500));
    mount();

    fireEvent.click(await screen.findByRole('button', { name: /export/i }));
    expect(await screen.findByText(/export failed \(HTTP 500\)/i)).toBeInTheDocument();
  });

  it('is offered to a reader — the export carries nothing the page does not show', async () => {
    server.use(
      http.get('/api/v1/auth/me', () =>
        HttpResponse.json({
          id: 2,
          username: 'viewer',
          role: 'user',
          is_active: true,
          is_admin: false,
          groups: [{ id: 2, name: 'Viewers' }],
          permissions: ['projects:read'],
          created_at: '2024-01-01T00:00:00Z',
        }),
      ),
    );
    const me = vi.spyOn(api, 'getCurrentUser');
    mount();

    await waitFor(() => expect(me).toHaveBeenCalled());
    expect(screen.getByRole('button', { name: /export/i })).toBeInTheDocument();
  });
});
