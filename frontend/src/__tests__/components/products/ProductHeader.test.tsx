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
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
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
    expect(await screen.findByText(/link a file/i)).toBeInTheDocument();
  });
});
