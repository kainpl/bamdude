/**
 * The catalog menu's «To order…» (WS-13 E5 G01, E8 F02): a catalog, active product, for
 * somebody who may change orders — and the host opens the dialog on that product.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useRef } from 'react';
import { fireEvent, screen } from '@testing-library/react';
import { render } from '../../utils';
import type { Permission, ProductListItem } from '../../../api/client';
import { ProductActionMenu } from '../../../components/products/ProductActionMenu';
import { useProductActions } from '../../../components/products/productActions/useProductActions';
import { PRODUCT_ROW_DEFAULTS } from '../../wireDefaults';

const auth = vi.hoisted(() => ({ granted: new Set<string>() }));
vi.mock('../../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({ ...actual.useAuth(), hasPermission: (p: Permission) => auth.granted.has(p) }),
  };
});
// The dialog itself is E5's; here only which product it opens on matters.
vi.mock('../../../components/projects/add-to-order/AddToOrderDialog', () => ({
  AddToOrderDialog: ({ preselectProduct }: { preselectProduct: { id: number; code: string; hasGroups?: boolean } }) => (
    <div role="dialog" aria-label="Add to order" data-has-groups={String(preselectProduct.hasGroups)}>
      {preselectProduct.code}
    </div>
  ),
}));

const product: ProductListItem = {
  ...PRODUCT_ROW_DEFAULTS,
  id: 4,
  code: 'PR-0004',
  name: 'Flask',
  is_active: true,
  sku: null,
  version: null,
  category: null,
  status: 'ready',
  origin: 'catalog',
  origin_file_id: null,
  origin_plate_index: null,
  cover_image_filename: null,
  has_cover: false,
  parts_count: 1,
  plates_count: 1,
  lines_count: 0,
  kits_available: 0,
  finished_available: 0,
  materials: [],
  colors: [],
  models: [],
};

function Menu({ over }: { over: Partial<ProductListItem> }) {
  const heading = useRef<HTMLHeadingElement>(null);
  const actions = useProductActions<ProductListItem>({ context: 'catalog', onEdit: () => {}, fallbackFocusRef: heading });
  return (
    <>
      <ProductActionMenu product={{ ...product, ...over }} actions={actions} />
      {actions.host}
    </>
  );
}

function open(over: Partial<ProductListItem> = {}) {
  render(<Menu over={over} />);
  fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
}

describe('ProductActionMenu — To order…', () => {
  beforeEach(() => {
    auth.granted = new Set(['orders:update', 'products:update', 'customers:update', 'stock:move', 'stock:adjust']);
  });

  it('opens the dialog on this product', async () => {
    open();
    fireEvent.click(screen.getByRole('menuitem', { name: 'To order…' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add to order' });
    expect(dialog).toHaveTextContent('PR-0004');
    // The row says whether the product has groups: the dialog need not wait for a list (review M-h).
    expect(dialog).toHaveAttribute('data-has-groups', 'false');
  });

  it('is not offered for a hidden product', () => {
    open({ is_active: false });
    expect(screen.queryByRole('menuitem', { name: 'To order…' })).not.toBeInTheDocument();
  });

  it('is not offered for a one-off product', () => {
    open({ origin: 'adhoc_job' });
    expect(screen.queryByRole('menuitem', { name: 'To order…' })).not.toBeInTheDocument();
  });

  it('is not offered to somebody who may not change orders', () => {
    auth.granted = new Set();
    open();
    expect(screen.queryByRole('menuitem', { name: 'To order…' })).not.toBeInTheDocument();
  });
});
