/**
 * URL/param construction of the server-driven spool list client fns
 * (task 4, 2026-08-29 server-driven-lists): getSpoolsPaged /
 * getSpoolGroupsPaged / getSpoolIds / getSpoolFacets — plus the legacy
 * `getSpools` pin (its four remaining consumers depend on the bare
 * include_archived call NEVER growing a `page`, which is the envelope
 * switch server-side).
 */

import { describe, it, expect, beforeAll, afterAll, afterEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { api } from '../../api/client';

const server = setupServer();
let captured: URL[] = [];

beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }));
afterEach(() => {
  server.resetHandlers();
  captured = [];
});
afterAll(() => server.close());

const emptyPage = { items: [], meta: { total: 0, current_page: 1, per_page: 50, last_page: 1 } };

/** `unknown` on purpose: a handful of callers hand this a deliberately
 *  malformed page to prove the client survives it, which is exactly what msw's
 *  `JsonBodyType` refuses. The widening is here, once. */
type JsonBody = Parameters<typeof HttpResponse.json>[0];

function capture(path: string, body: unknown) {
  server.use(
    http.get(path, ({ request }) => {
      captured.push(new URL(request.url));
      return HttpResponse.json(body as JsonBody);
    }),
  );
}

describe('getSpoolsPaged', () => {
  it('always sends page (the envelope switch), defaulting to 1', async () => {
    capture('/api/v1/inventory/spools', emptyPage);
    await api.getSpoolsPaged();
    expect(captured[0].searchParams.get('page')).toBe('1');
  });

  it('serializes every filter param and repeats the raw colour lists', async () => {
    capture('/api/v1/inventory/spools', emptyPage);
    await api.getSpoolsPaged({
      archived: 'active',
      usage: 'lowstock',
      material: 'PLA',
      brand: 'eSun',
      colors: ['Jade White', 'A00-W1'],
      color_rgbas: ['FFFFFFFF'],
      category: '__none__',
      catalog_id: 7,
      location_id: '3',
      stock: 'configured',
      assigned: 'unassigned',
      q: 'sun bl',
      sort_by: 'net_desc',
      page: 2,
      per_page: 48,
    });
    const p = captured[0].searchParams;
    expect(p.get('archived')).toBe('active');
    expect(p.get('usage')).toBe('lowstock');
    expect(p.get('material')).toBe('PLA');
    expect(p.get('brand')).toBe('eSun');
    expect(p.getAll('colors')).toEqual(['Jade White', 'A00-W1']);
    expect(p.getAll('color_rgbas')).toEqual(['FFFFFFFF']);
    expect(p.get('category')).toBe('__none__');
    expect(p.get('catalog_id')).toBe('7');
    expect(p.get('location_id')).toBe('3');
    expect(p.get('stock')).toBe('configured');
    expect(p.get('assigned')).toBe('unassigned');
    expect(p.get('q')).toBe('sun bl');
    expect(p.get('sort_by')).toBe('net_desc');
    expect(p.get('page')).toBe('2');
    expect(p.get('per_page')).toBe('48');
  });

  it('all=true replaces per_page and include_k_profiles is sent only when true', async () => {
    capture('/api/v1/inventory/spools', emptyPage);
    await api.getSpoolsPaged({ all: true, per_page: 48, include_k_profiles: true });
    let p = captured[0].searchParams;
    expect(p.get('all')).toBe('true');
    expect(p.has('per_page')).toBe(false);
    expect(p.get('include_k_profiles')).toBe('true');

    await api.getSpoolsPaged({ per_page: 24, include_k_profiles: false });
    p = captured[1].searchParams;
    expect(p.has('all')).toBe(false);
    expect(p.get('per_page')).toBe('24');
    expect(p.has('include_k_profiles')).toBe(false);
  });
});

describe('getSpoolGroupsPaged', () => {
  it('is the paged surface plus group_similar=true', async () => {
    capture('/api/v1/inventory/spools', emptyPage);
    await api.getSpoolGroupsPaged({ archived: 'active', sort_by: 'material_asc', page: 3, per_page: 24 });
    const p = captured[0].searchParams;
    expect(p.get('group_similar')).toBe('true');
    expect(p.get('archived')).toBe('active');
    expect(p.get('sort_by')).toBe('material_asc');
    expect(p.get('page')).toBe('3');
  });
});

describe('getSpoolIds', () => {
  it('keeps the filters + q and strips every paging/sort/include param', async () => {
    capture('/api/v1/inventory/spools/ids', { ids: [] });
    await api.getSpoolIds({
      archived: 'archived',
      material: 'PETG',
      q: 'sun',
      // A caller reusing its list params must not leak these — the ids
      // endpoint takes none of them.
      sort_by: 'net_desc',
      page: 4,
      per_page: 24,
      all: true,
      include_k_profiles: true,
    });
    const p = captured[0].searchParams;
    expect(p.get('archived')).toBe('archived');
    expect(p.get('material')).toBe('PETG');
    expect(p.get('q')).toBe('sun');
    expect(p.has('sort_by')).toBe(false);
    expect(p.has('page')).toBe(false);
    expect(p.has('per_page')).toBe(false);
    expect(p.has('all')).toBe(false);
    expect(p.has('include_k_profiles')).toBe(false);
  });
});

describe('getSpoolFacets', () => {
  it('sends the archived tab scope, or nothing at all', async () => {
    capture('/api/v1/inventory/spools/facets', {
      materials: [], brands: [], categories: [], catalog_ids: [], colors: [],
    });
    await api.getSpoolFacets('archived');
    expect(captured[0].searchParams.get('archived')).toBe('archived');

    await api.getSpoolFacets();
    expect(captured[1].search).toBe('');
  });
});

describe('getSpools — the legacy pin', () => {
  it('stays the bare include_archived call: no page, no envelope params', async () => {
    capture('/api/v1/inventory/spools', []);
    await api.getSpools(true);
    const p = captured[0].searchParams;
    expect(p.get('include_archived')).toBe('true');
    // `page` is the server's envelope switch — the four legacy consumers
    // (AssignSpoolModal, ConfigureAmsSlotModal, SpoolDisplayNameSettings,
    // SpoolFormModal) depend on the flat full shape this call returns.
    expect(p.has('page')).toBe(false);
  });
});
