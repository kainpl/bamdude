/**
 * One dialog, two sections — the fields the operator types and the gallery.
 *
 * The fields half is the pass-2 `ProductModal` unchanged, and that is what the
 * first two tests pin: a create still posts every field, an edit still PATCHes
 * only what moved. The gallery half exists only in edit mode, because a product
 * that does not exist yet has no id to hang an upload on.
 */

import { useState } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Product } from '../../../api/client';
import { ProductCardDialog } from '../../../components/products/ProductCardDialog';
import { ProductGallery } from '../../../components/products/ProductGallery';

const product = {
  id: 7,
  name: 'Flask',
  is_active: true,
  has_cover: false,
  cover_image_filename: null,
  parts_count: 0,
  plates_count: 0,
  lines_count: 0,
  description: 'A flask',
  notes: null,
  designer: 'Ada',
  license: null,
  source_url: null,
  design_id: null,
  attachments: [],
  parts: [],
  library_file_ids: [],
  library_folder_ids: [],
  units_printed_total: 0,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
} as unknown as Product;

const noop = () => {};

/** The dialog the way a page opens it — a control that mounts it and takes it
 *  away again, which is the only shape in which "the focus comes back" can be
 *  observed at all. */
function Openable() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open card
      </button>
      {open && <ProductCardDialog product={product} onClose={() => setOpen(false)} />}
    </>
  );
}

describe('ProductCardDialog', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('creates a product from the fields section', async () => {
    const create = vi.spyOn(api, 'createProduct').mockResolvedValue(product as never);
    render(<ProductCardDialog product={null} onClose={noop} />);

    fireEvent.change(screen.getByLabelText(/^name$/i), { target: { value: 'Lid' } });
    fireEvent.change(screen.getByLabelText(/designer/i), { target: { value: 'Ada' } });
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }));

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({
        name: 'Lid',
        description: null,
        designer: 'Ada',
        license: null,
        source_url: null,
        design_id: null,
        notes: null,
      }),
    );
  });

  it('sends only the field that changed on an edit', async () => {
    const update = vi.spyOn(api, 'updateProduct').mockResolvedValue(product as never);
    render(<ProductCardDialog product={product} onClose={noop} />);

    fireEvent.change(screen.getByLabelText(/licence|license/i), { target: { value: 'CC-BY' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(update).toHaveBeenCalledWith(7, { license: 'CC-BY' }));
  });

  it('carries the gallery in edit mode and not in create mode', () => {
    // ⚠️ `-dialog`: the product page renders its own gallery and this dialog
    // opens OVER it, so the two would otherwise answer the same testid.
    const { unmount } = render(<ProductCardDialog product={product} onClose={noop} />);
    expect(screen.getByTestId('product-gallery-dialog')).toBeInTheDocument();
    expect(screen.queryByTestId('product-gallery')).not.toBeInTheDocument();
    unmount();

    render(<ProductCardDialog product={null} onClose={noop} />);
    expect(screen.queryByTestId('product-gallery-dialog')).not.toBeInTheDocument();
  });

  it('is a modal dialog named by its heading, and hands the focus back on Escape', async () => {
    render(<Openable />);
    const opener = screen.getByRole('button', { name: 'open card' });
    opener.focus();
    fireEvent.click(opener);

    // The gallery inside carries no dialog role, so there is exactly one.
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName('Edit product');
    expect(dialog).toHaveFocus();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('names its own gallery apart from the page gallery underneath it', () => {
    // ⚠️ Two live galleries with the same accessible name is what a screen
    // reader hears when the card dialog opens over the product page: two
    // regions called "Pictures", one of which is the one being edited. The
    // testid suffix solved this for the tests; the heading solves it for the
    // people the page is for.
    render(
      <>
        <ProductGallery product={product} canEdit />
        <ProductCardDialog product={product} onClose={noop} />
      </>,
    );

    const page = screen.getByTestId('product-gallery');
    const dialog = screen.getByTestId('product-gallery-dialog');
    expect(within(page).getByRole('heading', { name: 'Pictures' })).toBeInTheDocument();
    expect(within(dialog).getByRole('heading', { name: 'Pictures of this product' })).toBeInTheDocument();
    expect(page).toHaveAccessibleName('Pictures');
    expect(dialog).toHaveAccessibleName('Pictures of this product');
  });
});
