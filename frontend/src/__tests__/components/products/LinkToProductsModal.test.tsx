/**
 * Linking a library file or folder to PRODUCTS.
 *
 * The File Manager used to hold two nearly identical inline modals that wrote
 * `project_ids`; both are gone and this one component serves file and folder
 * alike. The two rules worth pinning: the save writes `product_ids` through
 * the same update call the old modal used, and a product that has left the
 * catalog is still offered while this item is linked to it — hiding it would
 * render the chip row as "nothing chosen" and the next save would commit that
 * emptiness.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { LinkToProductsModal } from '../../../components/products/LinkToProductsModal';

describe('LinkToProductsModal', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('saves the chosen products as product_ids on the folder', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue([
      { id: 1, name: 'Flask', is_active: true },
      { id: 2, name: 'Lid', is_active: true },
    ] as never);
    const update = vi.spyOn(api, 'updateLibraryFolder').mockResolvedValue({} as never);

    render(
      <LinkToProductsModal
        kind="folder"
        item={{ id: 5, name: 'Flasks', products: [{ id: 1, name: 'Flask', is_active: true }] }}
        onClose={() => {}}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Lid' }));
    fireEvent.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(5, expect.objectContaining({ product_ids: [1, 2] })),
    );
  });

  it('saves a file through the file update call', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue([{ id: 1, name: 'Flask', is_active: true }] as never);
    const update = vi.spyOn(api, 'updateLibraryFile').mockResolvedValue({} as never);

    render(
      <LinkToProductsModal kind="file" item={{ id: 3, filename: 'a.3mf', products: [] }} onClose={() => {}} />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Flask' }));
    fireEvent.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(3, expect.objectContaining({ product_ids: [1] })),
    );
  });

  it('names the file the way the row does — print name over filename', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue([] as never);

    render(
      <LinkToProductsModal
        kind="file"
        item={{ id: 3, filename: '20260901_154302_lid.gcode.3mf', print_name: 'Flask lid v3', products: [] }}
        onClose={() => {}}
      />,
    );

    expect(await screen.findByText(/Flask lid v3/)).toBeInTheDocument();
    expect(screen.queryByText(/20260901_154302_lid/)).not.toBeInTheDocument();
  });

  it('keeps an inactive product that is already linked', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue([
      { id: 1, name: 'Flask', is_active: true },
      { id: 9, name: 'Retired', is_active: false },
    ] as never);

    render(
      <LinkToProductsModal
        kind="file"
        item={{ id: 3, filename: 'a.3mf', products: [{ id: 9, name: 'Retired', is_active: false }] }}
        onClose={() => {}}
      />,
    );

    expect(await screen.findByRole('button', { name: /Retired/ })).toBeInTheDocument();
  });

  it('unlinks from everything when every chip is deselected', async () => {
    vi.spyOn(api, 'getProducts').mockResolvedValue([{ id: 1, name: 'Flask', is_active: true }] as never);
    const update = vi.spyOn(api, 'updateLibraryFile').mockResolvedValue({} as never);

    render(
      <LinkToProductsModal
        kind="file"
        item={{ id: 3, filename: 'a.3mf', products: [{ id: 1, name: 'Flask', is_active: true }] }}
        onClose={() => {}}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Flask' }));
    fireEvent.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => expect(update).toHaveBeenCalledWith(3, { product_ids: [] }));
  });
});
