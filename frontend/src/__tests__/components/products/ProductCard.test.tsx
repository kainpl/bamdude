/**
 * `has_cover` is the EFFECTIVE cover — the explicit column or the first picture
 * — so the card never reads `cover_image_filename` to decide. A card that asked
 * the column would show the placeholder for every product whose cover is the
 * implicit first picture, which is most of them.
 */

import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../../utils';
import type { ProductListItem } from '../../../api/client';
import { ProductCard } from '../../../components/products/ProductCard';

const base: ProductListItem = {
  id: 4,
  name: 'Flask',
  is_active: true,
  cover_image_filename: null,
  has_cover: false,
  parts_count: 2,
  plates_count: 1,
  lines_count: 0,
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
