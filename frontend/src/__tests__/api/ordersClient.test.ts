/**
 * The orders / products / customers client — the shape of what goes on the
 * wire, not what comes back.
 *
 * `fetch` is stubbed rather than routed through MSW because what is under test
 * here IS the request: which filters were serialised, which body was built,
 * which method was used. MSW would answer them; a spy lets us read them.
 * `request()` prefixes `/api/v1` and adds its own headers, so the URL is
 * asserted with `toContain` rather than an equality on the whole string.
 */

import { describe, it, expect, vi, beforeEach, afterEach, type MockInstance } from 'vitest';
import { api } from '../../api/client';

function okJson(body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

describe('orders / products / customers client', () => {
  let fetchSpy: MockInstance<typeof globalThis.fetch>;

  beforeEach(() => {
    // Spying rather than assigning keeps MSW's own patched `fetch` — installed
    // globally in `setup.ts` — intact for every other file.
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() => okJson([]));
  });
  afterEach(() => {
    fetchSpy.mockRestore();
  });

  const urlOf = (call: number) => String(fetchSpy.mock.calls[call][0]);
  const initOf = (call: number) => fetchSpy.mock.calls[call][1] as RequestInit;

  it('getOrders sends only the filters that are set', async () => {
    await api.getOrders({ status: 'active', product_id: 7 });
    expect(urlOf(0)).toContain('/projects/?status=active&product_id=7');
  });

  it('getOrders sends an empty query when nothing is filtered', async () => {
    await api.getOrders();
    expect(urlOf(0)).toContain('/projects/?');
    expect(urlOf(0)).not.toContain('status=');
    expect(urlOf(0)).not.toContain('customer_id=');
  });

  it('addArchivesToOrder sends a null line when none is chosen', async () => {
    await api.addArchivesToOrder(3, [1, 2]);
    expect(urlOf(0)).toContain('/projects/3/add-archives');
    expect(JSON.parse(String(initOf(0).body))).toEqual({
      archive_ids: [1, 2],
      project_line_id: null,
    });
  });

  it('addArchivesToOrder carries the chosen line', async () => {
    await api.addArchivesToOrder(3, [1], 9);
    expect(JSON.parse(String(initOf(0).body))).toEqual({
      archive_ids: [1],
      project_line_id: 9,
    });
  });

  it('removeProductPartAlias encodes the key in the query string', async () => {
    await api.removeProductPartAlias(1, 2, 'lid a.stl');
    expect(urlOf(0)).toContain('/products/1/parts/2/aliases?name_key=lid%20a.stl');
    expect(initOf(0).method).toBe('DELETE');
  });

  it('getProducts omits an unset active filter', async () => {
    await api.getProducts({ q: 'lid' });
    expect(urlOf(0)).toContain('/products/?q=lid');
    expect(urlOf(0)).not.toContain('active=');
  });

  it('getProducts sends active=false, which is a filter and not an absence', async () => {
    await api.getProducts({ active: false });
    expect(urlOf(0)).toContain('/products/?active=false');
  });

  it('createProductFromFile posts with no body', async () => {
    await api.createProductFromFile(12);
    expect(urlOf(0)).toContain('/products/from-file/12');
    expect(initOf(0).method).toBe('POST');
    expect(initOf(0).body).toBeUndefined();
  });

  it('duplicateOrder sends a null name when none is given', async () => {
    await api.duplicateOrder(4);
    expect(urlOf(0)).toContain('/projects/4/duplicate');
    expect(JSON.parse(String(initOf(0).body))).toEqual({ name: null });
  });

  it('updateOrderProcurement patches the acquired count for one part', async () => {
    await api.updateOrderProcurement(5, 6, 3);
    expect(urlOf(0)).toContain('/projects/5/procurement/6');
    expect(initOf(0).method).toBe('PATCH');
    expect(JSON.parse(String(initOf(0).body))).toEqual({ quantity_acquired: 3 });
  });

  it('setProductFiles replaces the whole link set with PUT', async () => {
    await api.setProductFiles(2, [10, 11]);
    expect(urlOf(0)).toContain('/products/2/files');
    expect(initOf(0).method).toBe('PUT');
    expect(JSON.parse(String(initOf(0).body))).toEqual({ library_file_ids: [10, 11] });
  });

  it('getFoldersByProduct reads the library, not the products router', async () => {
    await api.getFoldersByProduct(8);
    expect(urlOf(0)).toContain('/library/folders/by-product/8');
  });

  it('getCustomers reads the collection', async () => {
    await api.getCustomers();
    expect(urlOf(0)).toContain('/customers/');
  });
});
