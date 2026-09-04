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
import { useQueryClient } from '@tanstack/react-query';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { LinkToProductsModal } from '../../../components/products/LinkToProductsModal';

/** Somebody retires a product in another tab: the catalog query is invalidated
 *  farm-wide and comes back with the row flagged inactive. */
function Retire() {
  const queryClient = useQueryClient();
  return (
    <button type="button" data-testid="retire" onClick={() => queryClient.invalidateQueries({ queryKey: ['products'] })}>
      retire
    </button>
  );
}

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

  it('seeds the selection from product_ids when the row came from the file LIST', async () => {
    // The list response carries ids, not refs. Reading only `products` would
    // open the dialog with nothing ticked and the next save would write that
    // emptiness over the file's real links.
    vi.spyOn(api, 'getProducts').mockResolvedValue([
      { id: 1, name: 'Flask', is_active: true },
      { id: 2, name: 'Lid', is_active: true },
    ] as never);
    const update = vi.spyOn(api, 'updateLibraryFile').mockResolvedValue({} as never);

    render(
      <LinkToProductsModal kind="file" item={{ id: 3, filename: 'a.3mf', product_ids: [2] }} onClose={() => {}} />,
    );

    fireEvent.click(await screen.findByRole('button', { name: /save/i }));

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(3, expect.objectContaining({ product_ids: [2] })),
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
  it('a product retired AFTER the dialog opened stays on offer', async () => {
    // ⚠️ The keep-set is frozen at MOUNT — one rule, one hook (`useBoundIds`),
    // shared with `ProductPicker`. Read live, the chip for a product somebody
    // retires while this dialog is open would vanish under the operator's
    // hand, and the save that follows would commit the missing link.
    const get = vi
      .spyOn(api, 'getProducts')
      .mockResolvedValueOnce([{ id: 1, name: 'Flask', is_active: true }] as never)
      .mockResolvedValue([{ id: 1, name: 'Flask', is_active: false }] as never);

    render(
      <>
        <Retire />
        <LinkToProductsModal
          kind="file"
          item={{ id: 3, filename: 'a.3mf', products: [{ id: 1, name: 'Flask', is_active: true }] }}
          onClose={() => {}}
        />
      </>,
    );

    expect(await screen.findByRole('button', { name: 'Flask' })).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('retire'));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));

    expect(screen.getByRole('button', { name: 'Flask' })).toBeInTheDocument();
  });

  it('keeps an unticked inactive product offered, so the operator can change their mind', async () => {
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

    // Untick it: keyed off the live selection the chip would delete itself.
    fireEvent.click(await screen.findByRole('button', { name: /Retired/ }));
    expect(screen.getByRole('button', { name: /Retired/ })).toBeInTheDocument();
  });
});
