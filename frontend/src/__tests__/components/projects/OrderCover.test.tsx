/**
 * The cache-buster must not assume the token is there.
 *
 * `getProjectCoverImageUrl` runs the URL through `withStreamToken`, which
 * returns it BARE until the stream token has loaded — so on a cold page there
 * is no `?` yet. A hard-coded `&v=` made that first render request
 * `…/cover-image&v=0`, a 404 that `rewriteMediaSrcWithToken` cannot repair
 * because it has no query string to append to.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order } from '../../../api/client';
import { OrderCover } from '../../../components/projects/OrderCover';

const order = { id: 1, cover_image_filename: 'cover.jpg' } as unknown as Order;

describe('OrderCover', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('opens the query string itself when the stream token has not loaded yet', () => {
    vi.spyOn(api, 'getProjectCoverImageUrl').mockReturnValue('/api/v1/projects/1/cover-image');

    render(<OrderCover order={order} canEdit />);

    const src = screen.getByTestId('order-cover-image').getAttribute('src') ?? '';
    expect(src).toContain('?v=');
    expect(src).not.toContain('image&v=');
  });

  it('appends to the query string the token already opened', () => {
    vi.spyOn(api, 'getProjectCoverImageUrl').mockReturnValue('/api/v1/projects/1/cover-image?token=x');

    render(<OrderCover order={order} canEdit />);

    const src = screen.getByTestId('order-cover-image').getAttribute('src') ?? '';
    expect(src).toContain('token=x');
    expect(src).toContain('&v=');
  });

  it('shows nothing at all to a viewer of an order with no cover', () => {
    // `null`, not an empty fragment: React renders both as nothing, and only
    // one of them says so to the next reader.
    render(
      <div data-testid="cover-slot">
        <OrderCover order={{ id: 1, cover_image_filename: null } as unknown as Order} canEdit={false} />
      </div>,
    );

    expect(screen.queryByTestId('order-cover-image')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.getByTestId('cover-slot')).toBeEmptyDOMElement();
  });
});
