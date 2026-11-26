/**
 * C047 part 2: what a Spoolman delete or a refused change of the Spoolman source carries
 * through the client — the code, the reason (`spoolman_print_in_progress`), the spool a
 * create-not-bound names, the reasons of `spool_in_use` — so the UI picks its text by
 * them and never by the translated sentence. Read through the real `request()` path.
 */

import { describe, it, expect, vi, afterEach, type MockInstance } from 'vitest';
import type { TFunction } from 'i18next';
import { api, ApiError } from '../../api/client';
import { spoolmanSourceRefusalText } from '../../utils/spoolmanSourceRefusal';

function answer(status: number, body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
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

const t = ((key: string) => key) as unknown as TFunction;

describe('Spoolman delete and source refusals', () => {
  let fetchSpy: MockInstance<typeof globalThis.fetch>;
  afterEach(() => fetchSpy.mockRestore());

  it('a refused source change keeps its reason, and the text follows it', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      answer(409, {
        detail: { error: 'spoolman_print_in_progress', reason: 'charge_window', message: 'A print is being charged…' },
      }),
    );
    const e = await refusalOf(api.keepPreviousSpoolmanUsageSource());
    expect(e.code).toBe('spoolman_print_in_progress');
    expect(e.reason).toBe('charge_window');
    expect(spoolmanSourceRefusalText(e, t)).toBe('settings.spoolmanSourceRefused.charge_window');
  });

  it('a print accounting in Spoolman has its own text; other errors are not this refusal', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      answer(409, { detail: { error: 'spoolman_print_in_progress', reason: 'print_accounting', message: 'x' } }),
    );
    const e = await refusalOf(api.keepPreviousSpoolmanUsageSource());
    expect(spoolmanSourceRefusalText(e, t)).toBe('settings.spoolmanSourceRefused.print_accounting');
    expect(spoolmanSourceRefusalText(new Error('boom'), t)).toBeNull();
  });

  it('a mode switch while spools are assigned to slots has its own text (A08)', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      answer(409, { detail: { error: 'spoolman_print_in_progress', reason: 'slot_writes', message: 'x' } }),
    );
    const e = await refusalOf(api.updateSpoolmanSettings({ spoolman_enabled: 'false' }));
    expect(spoolmanSourceRefusalText(e, t)).toBe('settings.spoolmanSourceRefused.slot_writes');
  });

  it('a spool in use names its reasons', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      answer(409, {
        detail: { error: 'spool_in_use', message: 'used', reasons: ['usage_history', 7, 'print_tag'] },
      }),
    );
    const e = await refusalOf(api.deleteSpoolmanInventorySpool(42));
    expect(e.code).toBe('spool_in_use');
    expect(e.spoolInUseReasons).toEqual(['usage_history', 'print_tag']);
  });

  it('a create that a concurrent delete beat names the created spool', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      answer(409, { detail: { error: 'spool_created_not_bound', spool_id: 57, message: 'created, not bound' } }),
    );
    const e = await refusalOf(api.createSpoolmanSpoolFromSlot({ printer_id: 1, ams_id: 0, tray_id: 1 }));
    expect(e.code).toBe('spool_created_not_bound');
    expect(e.spoolId).toBe(57);
  });

  it('a confirmed delete returns its cleanup warning', async () => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      answer(200, { status: 'deleted', warning: { code: 'slot_kept_feeding', slots: [{ printer_id: 1, ams_id: 0, tray_id: 2 }] } }),
    );
    const result = await api.deleteSpoolmanInventorySpool(42);
    expect(result.warning?.code).toBe('slot_kept_feeding');
    expect(result.warning?.slots).toEqual([{ printer_id: 1, ams_id: 0, tray_id: 2 }]);
  });
});
