/**
 * The tabs are navigation between three sibling roots, not `?tab=` state — so
 * the active one is read from the route and the other two are real links a user
 * can copy. `render` from `__tests__/utils` wraps in a BrowserRouter, so the
 * route is set the way the other route-aware tests set it: pushState first.
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

  it('does not light the Orders tab on an order detail page', () => {
    window.history.pushState({}, '', '/projects/12');
    render(<ProjectsTabs />);
    expect(screen.getByRole('link', { name: /orders/i })).not.toHaveAttribute('aria-current');
    expect(screen.getByRole('link', { name: /orders/i })).not.toHaveClass('border-bambu-green');
    expect(screen.getByRole('link', { name: /products/i })).not.toHaveAttribute('aria-current');
    expect(screen.getByRole('link', { name: /customers/i })).not.toHaveAttribute('aria-current');
  });

  it('marks the products tab current on a product detail page', () => {
    window.history.pushState({}, '', '/products/3');
    render(<ProjectsTabs />);
    expect(screen.getByRole('link', { name: /products/i })).toHaveAttribute('aria-current', 'page');
  });
});
