/**
 * `has_cover` is the EFFECTIVE cover — the explicit column or the first picture
 * — so the card never reads `cover_image_filename` to decide. A card that asked
 * the column would show the placeholder for every product whose cover is the
 * implicit first picture, which is most of them.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { strayZeroTextNodes } from '../../domHelpers';
import { api, ApiError } from '../../../api/client';
import type { ProductListItem } from '../../../api/client';
import { ProductCard } from '../../../components/products/ProductCard';

const base: ProductListItem = {
  id: 4,
  name: 'Flask',
  is_active: true,
  origin: 'catalog',
  origin_file_id: null,
  origin_plate_index: null,
  cover_image_filename: null,
  has_cover: false,
  parts_count: 2,
  plates_count: 1,
  lines_count: 0,
  kits_available: 0,
};

const noop = () => {};

function mount(over: Partial<ProductListItem> = {}) {
  render(
    <ProductCard
      product={{ ...base, ...over }}
      onEdit={noop}
      onDuplicate={noop}
      onToggleActive={noop}
      onDelete={noop}
    />,
  );
}

describe('ProductCard cover', () => {
  it('renders the cover image when the product has one', () => {
    mount({ has_cover: true });

    expect(screen.getByTestId('product-cover')).toHaveAttribute(
      'src',
      expect.stringContaining('/products/4/cover-image'),
    );
    expect(screen.queryByTestId('product-cover-placeholder')).not.toBeInTheDocument();
  });

  it('falls back to the neutral tile when it has none', () => {
    mount();

    expect(screen.getByTestId('product-cover-placeholder')).toBeInTheDocument();
    expect(screen.queryByTestId('product-cover')).not.toBeInTheDocument();
  });

  it('does not read the explicit column — an implicit cover is still a cover', () => {
    mount({ has_cover: true, cover_image_filename: null });
    expect(screen.getByTestId('product-cover')).toBeInTheDocument();
  });
});

describe('ProductCard export', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('downloads the product as a ZIP from the card menu', async () => {
    const save = vi.spyOn(api, 'downloadProductExport').mockResolvedValue(undefined);
    mount();

    fireEvent.click(await screen.findByTestId('product-menu'));
    fireEvent.click(screen.getByRole('menuitem', { name: /export/i }));

    await waitFor(() => expect(save).toHaveBeenCalledWith(4));
  });

  it('names the trigger as a menu before it is opened', async () => {
    // The popup carries `role="menu"`, but a screen reader meets this button
    // first; without these it is announced as an ordinary button and nothing
    // says a menu opens, or that one is open.
    mount();

    const trigger = await screen.findByTestId('product-menu');
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
  });

  it('says so when the export is refused, and stays on the list', async () => {
    vi.spyOn(api, 'downloadProductExport').mockRejectedValue(new ApiError('nope', 500));
    mount();

    fireEvent.click(await screen.findByTestId('product-menu'));
    fireEvent.click(screen.getByRole('menuitem', { name: /export/i }));

    expect(await screen.findByText(/export failed \(HTTP 500\)/i)).toBeInTheDocument();
  });
});

/**
 * ⚠️ A `<button>` inside an `<a>` is invalid HTML, and the menu used to be
 * exactly that: every item cancelled the navigation its own click caused, so
 * one item added without the guard navigated instead of acting. The panel now
 * lives on `document.body` and the card's anchor is an overlay.
 */
describe('ProductCard menu placement', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the open menu outside the card anchor, on document.body', async () => {
    mount();

    fireEvent.click(await screen.findByTestId('product-menu'));

    const panel = screen.getByRole('menu');
    expect(panel.parentElement).toBe(document.body);
    expect(screen.getByRole('link').contains(panel)).toBe(false);
    expect(screen.getByTestId('product-4-card').querySelector('[role="menu"]')).toBeNull();
  });

  it('closes on Escape', async () => {
    mount();

    fireEvent.click(await screen.findByTestId('product-menu'));
    expect(screen.getByRole('menu')).toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});

describe('ProductCard free-stock badge', () => {
  it('shows the kits the shelf can already make', () => {
    // The number rides along on the LIST response, so nothing is fetched to
    // decide — and the badge is the whole reading of it (pass 8, Decision 6).
    mount({ kits_available: 3 });

    expect(screen.getByTestId('product-kits-badge')).toHaveTextContent('3 kits in stock');
  });

  it('says nothing at all about an empty shelf', () => {
    // Every product in the catalog would otherwise carry "0 kits in stock".
    // ⚠️ `> 0`, never a bare `&&` on the number — `{0 && …}` renders the 0.
    mount({ kits_available: 0 });

    expect(screen.queryByTestId('product-kits-badge')).not.toBeInTheDocument();
    expect(strayZeroTextNodes(screen.getByTestId('product-4-card'))).toHaveLength(0);
  });
});
