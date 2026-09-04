/**
 * The tabs are navigation between three sibling roots, not `?tab=` state — so
 * the active one is read from the route and the other two are real links a user
 * can copy. `render` from `__tests__/utils` wraps in a BrowserRouter, so the
 * route is set the way the other route-aware tests set it: pushState first.
 *
 * ⚠️ **The test that pinned `/products/3` as LIT is gone (pass 6).** Only the
 * three LIST pages render this nav — `OrdersPage`, `ProductsPage`,
 * `CustomersPage`, grep it — so no detail route ever reaches these regexes,
 * and that test pinned an unreachable state in the OPPOSITE direction from
 * the Orders tab. That contradiction WAS the finding. The three regexes now
 * agree, and the case below pins the agreement itself: the lookaheads are
 * code, and code nothing pins is code the next reader deletes as redundant.
 */

import { describe, it, expect, afterEach } from 'vitest';
import { screen } from '@testing-library/react';

import { render } from '../../utils';
import { ProjectsTabs } from '../../../components/projects/ProjectsTabs';

afterEach(() => {
  window.history.pushState({}, '', '/');
});

describe('ProjectsTabs', () => {
  it('marks the products tab on /products and links the other two', () => {
    window.history.pushState({}, '', '/products');
    render(<ProjectsTabs />);
    expect(screen.getByRole('link', { name: /products/i })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: /orders/i })).toHaveAttribute('href', '/projects');
    expect(screen.getByRole('link', { name: /customers/i })).toHaveAttribute('href', '/customers');
  });

  it('lights no tab on a detail path, whichever of the three it is', () => {
    for (const path of ['/projects/12', '/products/3', '/customers/3']) {
      window.history.pushState({}, '', path);
      const { unmount } = render(<ProjectsTabs />);
      for (const name of [/orders/i, /products/i, /customers/i]) {
        expect(screen.getByRole('link', { name })).not.toHaveAttribute('aria-current');
        expect(screen.getByRole('link', { name })).not.toHaveClass('border-bambu-green');
      }
      unmount();
    }
  });
});
