/**
 * Only a DIRECT file link can be unlinked from here, and the difference is not
 * cosmetic.
 *
 * `DELETE /products/{id}/files/{file_id}` does not refuse a folder-derived
 * file: `routes/products.py::unlink_file` reads the file's own rows in the
 * `product_files` pivot, subtracts this product and syncs the result. A file
 * that reached the product through a folder has no row there, so the set comes
 * back unchanged, the sync writes nothing and the endpoint answers 200 with the
 * product. Offering the × on such a chip therefore produces a button that
 * "works", refetches, and leaves the chip exactly where it was — the worst
 * shape a failure can take, because nothing at all is said.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Product } from '../../../api/client';
import { LinkedFiles } from '../../../components/products/LinkedFiles';

/** File 7 is linked to the product itself; file 8 only arrives through folder 3. */
const product = {
  id: 5,
  name: 'Flask',
  library_file_ids: [7],
  library_folder_ids: [3],
} as unknown as Product;

describe('LinkedFiles', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([
      { id: 7, filename: 'flask.3mf' },
      { id: 8, filename: 'lid.3mf' },
    ] as never);
    vi.spyOn(api, 'getFoldersByProduct').mockResolvedValue([{ id: 3, name: 'Flasks' }] as never);
  });

  it('unlinks a directly linked file', async () => {
    const unlink = vi.spyOn(api, 'unlinkProductFile').mockResolvedValue(product as never);
    render(<LinkedFiles product={product} canEdit />);

    fireEvent.click(await screen.findByRole('button', { name: /unlink: flask\.3mf/i }));
    await waitFor(() => expect(unlink).toHaveBeenCalledWith(5, 7));
  });

  it('offers no unlink for a file that arrived through a folder, and says why', async () => {
    render(<LinkedFiles product={product} canEdit />);

    expect(await screen.findByText('lid.3mf')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /unlink: lid\.3mf/i })).not.toBeInTheDocument();
    expect(screen.getByText(/via folder/i)).toBeInTheDocument();
  });

  it('unlinks the folder that brought the file in', async () => {
    const unlink = vi.spyOn(api, 'unlinkProductFolder').mockResolvedValue(product as never);
    render(<LinkedFiles product={product} canEdit />);

    fireEvent.click(await screen.findByRole('button', { name: /unlink: flasks/i }));
    await waitFor(() => expect(unlink).toHaveBeenCalledWith(5, 3));
  });

  it('says nothing is linked only once both lists have answered', async () => {
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([] as never);
    vi.spyOn(api, 'getFoldersByProduct').mockResolvedValue([] as never);
    render(<LinkedFiles product={{ ...product, library_file_ids: [], library_folder_ids: [] }} canEdit />);

    // Not on the first paint, when both queries are still in flight.
    expect(screen.queryByText(/nothing linked yet/i)).not.toBeInTheDocument();
    expect(await screen.findByText(/nothing linked yet/i)).toBeInTheDocument();
  });
});
