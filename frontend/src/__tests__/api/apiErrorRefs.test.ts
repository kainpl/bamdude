/**
 * `ApiError.refs` (WS-13 E10 R03): a refusal that names the element it is about —
 * `{error, message, group, option}` from the variants batch — keeps those references
 * through the client, so the dialog can point at the field instead of parsing a
 * translated sentence. Read through the real `request()` path: `fetch` answers, the
 * client parses.
 */

import { describe, it, expect, vi, afterEach, type MockInstance } from 'vitest';
import { api, ApiError } from '../../api/client';

function refusal(status: number, detail: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify({ detail }), { status, headers: { 'Content-Type': 'application/json' } }),
  );
}

async function refusalOf(call: Promise<unknown>): Promise<ApiError> {
  try {
    await call;
  } catch (e) {
    return e as ApiError;
  }
  throw new Error('the call did not refuse');
}

describe('ApiError.refs', () => {
  let fetchSpy: MockInstance<typeof globalThis.fetch>;
  afterEach(() => fetchSpy.mockRestore());

  it('a refusal naming a group and an option keeps both, beside its code and sentence', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      refusal(409, { error: 'option_in_use', message: '1 parts are bound to this option', group: 1, option: 11 }),
    );
    const e = await refusalOf(api.applyProductVariants(7, { revision: 'r', groups: [] }));
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(409);
    expect(e.code).toBe('option_in_use');
    expect(e.message).toBe('1 parts are bound to this option');
    expect(e.refs).toEqual({ group: 1, option: 11 });
  });

  it('a temp id is a reference too; a null one is not', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      refusal(409, { error: 'group_name_taken', message: 'A group with this name already exists', group: 't3', option: null }),
    );
    const e = await refusalOf(api.applyProductVariants(7, { revision: 'r', groups: [] }));
    expect(e.refs).toEqual({ group: 't3' });
  });

  it('a namesake refusal names the customer it means (WS-13 E11 A01)', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      refusal(409, { error: 'name_taken', message: 'A customer with this name already exists: CU-0003', customer: 3 }),
    );
    const e = await refusalOf(api.createCustomer({ name: 'Acme' }));
    expect(e.code).toBe('name_taken');
    expect(e.message).toBe('A customer with this name already exists: CU-0003');
    expect(e.refs).toEqual({ customer: 3 });
  });

  it('the flag of a knowingly made namesake goes with the request', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      Promise.resolve(new Response(JSON.stringify({ id: 9 }), { status: 200, headers: { 'Content-Type': 'application/json' } })),
    );
    await api.updateCustomer(9, { name: 'Acme', allow_duplicate_name: true });
    const init = fetchSpy.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(init.body as string)).toEqual({ name: 'Acme', allow_duplicate_name: true });
  });

  it('a refusal without references has none', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      refusal(409, { error: 'variants_changed', message: 'The variants changed', group: null, option: null }),
    );
    const e = await refusalOf(api.applyProductVariants(7, { revision: 'r', groups: [] }));
    expect(e.code).toBe('variants_changed');
    expect(e.refs).toBeUndefined();
  });

  it('a closed combination keeps its reasons — the well-formed ones only (spec product-part-variant-combinations п. 25)', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      refusal(409, {
        error: 'combination_unavailable',
        message: 'That combination is not available',
        reasons: [
          { code: 'option_without_part', group: 'Lid', option: 'snap', part: null },
          { code: 'part_not_sliced', group: null, option: null, part: 'Body' },
          { code: 'something_else', group: 'Lid', option: null, part: null },
          'not a reason',
        ],
      }),
    );
    const e = await refusalOf(api.addOrderLines(5, []));
    expect(e.code).toBe('combination_unavailable');
    expect(e.message).toBe('That combination is not available');
    expect(e.reasons).toEqual([
      { code: 'option_without_part', group: 'Lid', option: 'snap', part: null },
      { code: 'part_not_sliced', group: null, option: null, part: 'Body' },
    ]);
    fetchSpy.mockImplementation(() => refusal(409, { error: 'variants_changed', message: 'The variants changed' }));
    expect((await refusalOf(api.applyProductVariants(7, { revision: 'r', groups: [] }))).reasons).toBeUndefined();
  });

  it('a sentence detail and a validation list carry no references', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() => refusal(404, 'Variant group not found'));
    expect((await refusalOf(api.applyProductVariants(7, { revision: 'r', groups: [] }))).refs).toBeUndefined();
    fetchSpy.mockImplementation(() => refusal(422, [{ loc: ['body', 'groups', 0, 'group'], msg: 'bad' }]));
    expect((await refusalOf(api.applyProductVariants(7, { revision: 'r', groups: [] }))).refs).toBeUndefined();
  });
});
