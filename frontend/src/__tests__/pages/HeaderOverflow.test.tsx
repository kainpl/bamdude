/**
 * Two page headers must not overflow a phone viewport.
 *
 * A flex row whose items cannot shrink is as wide as its contents, and the
 * Spool Inventory header's action buttons come to far more than 390px side by
 * side. ⚠️ The consequence is not a clipped header: `<main>` is the scroll
 * container, so the whole page panned sideways with it.
 *
 * jsdom does no layout, so this asserts the two properties that make the
 * overflow impossible rather than a measured width — the header stacks below
 * `sm`, and the action group wraps. Desktop is unchanged, which is the other
 * half of the contract and just as easy to break.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import InventoryPageRouter from '../../pages/InventoryPage';
import { server } from '../mocks/server';
import systemInfoSource from '../../pages/SystemInfoPage.tsx?raw';

/**
 * The header row above a page's `h1` — the flex container that stacks below
 * `sm` and spreads out from `sm` up.
 *
 * ⚠️ Found by what it IS, not by counting parents. This was three
 * `parentElement` hops until the page titles were unified and the icon moved
 * inside the `h1`, which removed a level and silently walked the search up to
 * the page root — two assertions about a header, made against a `<div>` that
 * was never one. The classes below are the contract; the nesting around them
 * is not.
 */
function headerOf(heading: HTMLElement): HTMLElement {
  for (let node = heading.parentElement; node; node = node.parentElement) {
    if (node.className.includes('sm:flex-row')) return node;
  }
  throw new Error(`no header row above "${heading.textContent}" — nothing had sm:flex-row`);
}

describe('page headers do not widen past the viewport', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/settings/spoolman', () =>
        HttpResponse.json({
          spoolman_enabled: 'false',
          spoolman_url: '',
          spoolman_sync_mode: 'auto',
          spoolman_disable_weight_sync: 'false',
          spoolman_report_partial_usage: 'true',
        }),
      ),
      http.get('/api/v1/inventory/spools', () => HttpResponse.json([])),
      http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
      http.get('/api/v1/inventory/catalog', () => HttpResponse.json([])),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
    );
  });

  it('stacks the Spool Inventory header below sm and keeps it a row from sm up', async () => {
    render(<InventoryPageRouter />);

    const header = headerOf(await screen.findByRole('heading', { name: 'Spool Inventory' }));

    expect(header.className).toContain('flex-col');
    expect(header.className).toContain('sm:flex-row');
    // Desktop is unchanged: the row still spreads title and actions apart.
    expect(header.className).toContain('sm:justify-between');
  });

  it('lets the Spool Inventory actions wrap instead of widening the header', async () => {
    render(<InventoryPageRouter />);

    const header = headerOf(await screen.findByRole('heading', { name: 'Spool Inventory' }));
    const group = header.lastElementChild as HTMLElement;

    // ⚠️ Wrapping is the fix; hiding an action is not. Whatever the viewport,
    // without this the group is as wide as every button laid end to end.
    expect(group.className).toContain('flex-wrap');
    expect(group.querySelectorAll('button').length).toBeGreaterThan(0);
  });

  it('stacks the System Information header the same way', () => {
    // ⚠️ Asserted on the source, not on a render: that page reads a dozen
    // nested fields off /system/info before it draws anything, so pinning one
    // class string would cost a sixty-line fixture that pins nothing else. The
    // Inventory cases above exercise the same pattern against a real render.
    const header = systemInfoSource
      .split(/\r?\n/)
      .find((line) => line.includes('sm:justify-between') && line.includes('flex-col'));

    expect(header, 'the System Information header row').toBeDefined();
    expect(header).toContain('sm:flex-row');
    // items-start so the lone action keeps its own width while stacked rather
    // than stretching across the page.
    expect(header).toContain('items-start');
  });
});
