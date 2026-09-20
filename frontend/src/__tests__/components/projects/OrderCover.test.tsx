/**
 * The cover URL is the bare one the client builds — no cache-buster.
 *
 * It used to carry a `?v=` counter, because replacing a cover keeps the same
 * URL and the browser would otherwise show the old picture. The endpoint now
 * answers `Cache-Control: private, no-cache`, so the browser revalidates; a
 * second freshness rule in the component could only disagree with the first.
 * The counter also had to compute its own separator — `getProjectCoverImageUrl`
 * runs the URL through `withStreamToken`, which returns it BARE until the
 * stream token has loaded — and a hard-coded `&v=` had already produced
 * `…/cover-image&v=0` on a cold page once.
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

  it('renders the URL as it comes, with no token loaded yet', () => {
    vi.spyOn(api, 'getProjectCoverImageUrl').mockReturnValue('/api/v1/projects/1/cover-image');

    render(<OrderCover order={order} canEdit />);

    expect(screen.getByTestId('order-cover-image')).toHaveAttribute('src', '/api/v1/projects/1/cover-image');
  });

  it('adds nothing to the query string the token opened', () => {
    vi.spyOn(api, 'getProjectCoverImageUrl').mockReturnValue('/api/v1/projects/1/cover-image?token=x');

    render(<OrderCover order={order} canEdit />);

    const src = screen.getByTestId('order-cover-image').getAttribute('src') ?? '';
    expect(src).toBe('/api/v1/projects/1/cover-image?token=x');
    expect(src).not.toContain('v=');
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
