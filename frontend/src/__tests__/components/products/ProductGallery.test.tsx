/**
 * The gallery is the only place the cover rule is visible, and the rule has two
 * halves that look identical on screen: an EXPLICIT cover (the column) and the
 * implicit "first picture" default. "Clear cover" therefore only exists when
 * there is an explicit one to clear — offering it over the default would be a
 * button that does nothing and says it succeeded.
 *
 * Reorder posts the WHOLE ordered list of the category (the route accepts a
 * partial one, but a full list is the only form in which the two ends of a swap
 * cannot disagree), so the assertions name both filenames.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Product } from '../../../api/client';
import { ProductGallery } from '../../../components/products/ProductGallery';

const picture = (filename: string, original: string, sort: number) => ({
  category: 'pictures',
  filename,
  original_name: original,
  size: 1024,
  sort_order: sort,
  source: 'manual',
  source_file_id: null,
  uploaded_at: null,
});

const product = {
  id: 7,
  name: 'Flask',
  has_cover: true,
  cover_image_filename: null,
  attachments: [
    picture('a.png', 'front.png', 0),
    picture('b.png', 'back.png', 1),
    {
      category: 'bom_docs',
      filename: 'c.pdf',
      original_name: 'bom.pdf',
      size: 2048,
      sort_order: 0,
      source: 'manual',
      source_file_id: null,
      uploaded_at: null,
    },
  ],
} as unknown as Product;

describe('ProductGallery', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('picks a gallery picture as the cover', async () => {
    const set = vi.spyOn(api, 'setProductCover').mockResolvedValue({ status: 'success', filename: 'b.png' } as never);
    render(<ProductGallery product={product} canEdit />);

    fireEvent.click(screen.getByRole('button', { name: /set as cover: back\.png/i }));
    await waitFor(() => expect(set).toHaveBeenCalledWith(7, 'b.png'));
  });

  it('moving the first picture down rewrites the whole picture order', async () => {
    const reorder = vi.spyOn(api, 'reorderProductAttachments').mockResolvedValue([] as never);
    render(<ProductGallery product={product} canEdit />);

    fireEvent.click(screen.getByRole('button', { name: /move down: front\.png/i }));
    await waitFor(() => expect(reorder).toHaveBeenCalledWith(7, 'pictures', ['b.png', 'a.png']));
  });

  it('does not offer to move the first picture up or the last one down', () => {
    render(<ProductGallery product={product} canEdit />);

    expect(screen.queryByRole('button', { name: /move up: front\.png/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /move down: back\.png/i })).not.toBeInTheDocument();
  });

  it('uploads a picture into the pictures category', async () => {
    const upload = vi.spyOn(api, 'uploadProductAttachment').mockResolvedValue({} as never);
    render(<ProductGallery product={product} canEdit />);

    const file = new File(['x'], 'side.png', { type: 'image/png' });
    fireEvent.change(screen.getByTestId('gallery-upload-input'), { target: { files: [file] } });
    await waitFor(() => expect(upload).toHaveBeenCalledWith(7, file, 'pictures'));
  });

  it('uploads a dedicated cover through the cover route, not the gallery', async () => {
    const upload = vi.spyOn(api, 'uploadProductCover').mockResolvedValue({} as never);
    render(<ProductGallery product={product} canEdit />);

    const file = new File(['x'], 'hero.png', { type: 'image/png' });
    fireEvent.change(screen.getByTestId('gallery-cover-input'), { target: { files: [file] } });
    await waitFor(() => expect(upload).toHaveBeenCalledWith(7, file));
  });

  it('deletes a picture', async () => {
    const remove = vi.spyOn(api, 'deleteProductAttachment').mockResolvedValue([] as never);
    render(<ProductGallery product={product} canEdit />);

    fireEvent.click(screen.getByRole('button', { name: /remove picture: back\.png/i }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(7, 'b.png'));
  });

  it('offers "clear cover" only when an explicit cover was chosen', () => {
    const { unmount } = render(<ProductGallery product={product} canEdit />);
    expect(screen.queryByRole('button', { name: /clear cover/i })).not.toBeInTheDocument();
    unmount();

    render(<ProductGallery product={{ ...product, cover_image_filename: 'b.png' }} canEdit />);
    expect(screen.getByRole('button', { name: /clear cover/i })).toBeInTheDocument();
  });

  it('clears the explicit cover', async () => {
    const clear = vi.spyOn(api, 'deleteProductCover').mockResolvedValue({ status: 'success' } as never);
    render(<ProductGallery product={{ ...product, cover_image_filename: 'b.png' }} canEdit />);

    fireEvent.click(screen.getByRole('button', { name: /clear cover/i }));
    await waitFor(() => expect(clear).toHaveBeenCalledWith(7));
  });

  it('shows only pictures, and the cover tile above them', () => {
    render(<ProductGallery product={product} canEdit />);

    expect(screen.getAllByTestId(/^gallery-picture-/)).toHaveLength(2);
    expect(screen.queryByTestId('gallery-picture-c.pdf')).not.toBeInTheDocument();
    expect(screen.getByTestId('product-gallery-cover')).toHaveAttribute(
      'src',
      expect.stringContaining('/products/7/cover-image'),
    );
  });

  it('falls back to the placeholder tile when nothing can be a cover', () => {
    render(<ProductGallery product={{ ...product, has_cover: false, attachments: [] }} canEdit />);

    expect(screen.getByTestId('product-cover-placeholder')).toBeInTheDocument();
    expect(screen.queryByTestId('product-gallery-cover')).not.toBeInTheDocument();
  });

  it('opens a picture in the lightbox, steps to the next one and closes on Escape', () => {
    render(<ProductGallery product={product} canEdit />);

    fireEvent.click(screen.getByTestId('gallery-picture-a.png'));
    const lightbox = screen.getByTestId('gallery-lightbox');
    expect(lightbox).toBeInTheDocument();
    expect(screen.getByTestId('gallery-lightbox-image')).toHaveAttribute(
      'src',
      expect.stringContaining('attachment-image/a.png'),
    );

    fireEvent.click(screen.getByRole('button', { name: /next picture/i }));
    expect(screen.getByTestId('gallery-lightbox-image')).toHaveAttribute(
      'src',
      expect.stringContaining('attachment-image/b.png'),
    );

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('gallery-lightbox')).not.toBeInTheDocument();
  });

  it('marks the EFFECTIVE cover and refuses to set it again', () => {
    // No explicit column here, so the cover is the first picture by
    // `sort_order` — the same rule the server's `effective_cover` uses. A star
    // that only lit for the explicit column would be dark on most products.
    render(<ProductGallery product={product} canEdit />);

    expect(screen.getByRole('button', { name: /this is the cover: front\.png/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /set as cover: back\.png/i })).toBeEnabled();
  });

  it('marks the explicitly picked picture instead of the first one', () => {
    render(<ProductGallery product={{ ...product, cover_image_filename: 'b.png' }} canEdit />);

    expect(screen.getByRole('button', { name: /this is the cover: back\.png/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /set as cover: front\.png/i })).toBeEnabled();
  });

  it('marks no picture when the cover is a dedicated upload outside the gallery', () => {
    render(<ProductGallery product={{ ...product, cover_image_filename: 'cover_abc.png' }} canEdit />);

    expect(screen.queryByRole('button', { name: /this is the cover/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /set as cover: front\.png/i })).toBeEnabled();
  });

  it('never rewrites a src after a mutation — the cover is kept fresh by its own response', async () => {
    // `GET /products/{id}/cover-image` answers `Cache-Control: private,
    // no-cache`, so the browser revalidates that stable url on every render. A
    // version counter here would be a second, weaker answer to the same
    // question — and one `ProductCard` and `OrderCard`, which render the same
    // cover from the bare url, would not have. Every src stays plain.
    const set = vi.spyOn(api, 'setProductCover').mockResolvedValue({ status: 'success', filename: 'b.png' } as never);
    render(<ProductGallery product={product} canEdit />);

    const src = (testId: string) => screen.getByTestId(testId).getAttribute('src');
    const before = src('product-gallery-cover');
    const pictureSrc = screen.getByTestId('gallery-picture-a.png').querySelector('img')?.getAttribute('src');

    fireEvent.click(screen.getByRole('button', { name: /set as cover: back\.png/i }));
    await waitFor(() => expect(set).toHaveBeenCalled());

    expect(src('product-gallery-cover')).toBe(before);
    expect(before).toBe(api.getProductCoverImageUrl(7));
    expect(before).not.toContain('v=');
    expect(screen.getByTestId('gallery-picture-a.png').querySelector('img')?.getAttribute('src')).toBe(pictureSrc);
    expect(pictureSrc).not.toContain('v=');
  });

  it('breaks a tie on the filename, exactly as the server does', () => {
    // Two pictures at `sort_order: 0` is the ordinary case, not a corner one:
    // every upload into an empty category starts at 0, and a partial reorder
    // leaves the rest sharing a rank. The server's `sorted_attachments` orders
    // by `(sort_order, filename)`, so a gallery that stopped at `sort_order`
    // could star a different picture than `/cover-image` actually serves.
    render(
      <ProductGallery
        product={
          {
            ...product,
            attachments: [picture('z.png', 'zebra.png', 0), picture('a.png', 'apple.png', 0)],
          } as unknown as Product
        }
        canEdit
      />,
    );

    expect(screen.getAllByTestId(/^gallery-picture-/).map((el) => el.getAttribute('data-testid'))).toEqual([
      'gallery-picture-a.png',
      'gallery-picture-z.png',
    ]);
    expect(screen.getByRole('button', { name: /this is the cover: apple\.png/i })).toBeDisabled();
  });

  it('suffixes every testid so a page and the dialog over it never collide', () => {
    render(<ProductGallery product={product} canEdit testIdSuffix="-dialog" />);

    expect(screen.getByTestId('product-gallery-dialog')).toBeInTheDocument();
    expect(screen.getByTestId('gallery-picture-a.png-dialog')).toBeInTheDocument();
    expect(screen.queryByTestId('product-gallery')).not.toBeInTheDocument();
  });

  it('moves focus into the lightbox and gives it back on close', () => {
    render(<ProductGallery product={product} canEdit />);

    const opener = screen.getByTestId('gallery-picture-a.png');
    opener.focus();
    fireEvent.click(opener);

    expect(screen.getByTestId('gallery-lightbox')).toHaveFocus();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('gallery-lightbox')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('offers no edit control at all without the permission', () => {
    render(<ProductGallery product={product} canEdit={false} />);

    expect(screen.queryByRole('button', { name: /set as cover/i })).not.toBeInTheDocument();
    expect(screen.queryByTestId('gallery-upload-input')).not.toBeInTheDocument();
    // Looking is not editing — the pictures are still there.
    expect(screen.getAllByTestId(/^gallery-picture-/)).toHaveLength(2);
  });
});
