import { describe, expect, it, vi } from 'vitest';
import type { PrinterStatus } from '../../api/client';
import { createPrinterStatusBatcher } from '../../api/printerStatusBatch';

const row = (id: number, progress = 1) => ({ id, progress, connected: true } as PrinterStatus);

describe('fleet status REST reads', () => {
  it('turns 50 simultaneous reads into one request and keeps duplicates independent', async () => {
    const one = vi.fn(async (id: number) => row(id));
    const many = vi.fn(async (ids: number[]) => Object.fromEntries(ids.map(id => [id, row(id)])));
    const batch = createPrinterStatusBatcher(one, many, () => new Error('missing'));
    const result = await Promise.all([...Array.from({ length: 50 }, (_, id) => batch.get(id)), batch.get(3)]);
    expect(result.map(status => status.id)).toEqual([...Array.from({ length: 50 }, (_, id) => id), 3]);
    expect(many).toHaveBeenCalledTimes(1);
    expect(one).not.toHaveBeenCalled();
  });

  it('keeps newer MQTT fields when an old REST response arrives and still accepts enrichment', async () => {
    let finish!: (rows: Record<string, PrinterStatus>) => void;
    const many = vi.fn(() => new Promise<Record<string, PrinterStatus>>(resolve => { finish = resolve; }));
    const batch = createPrinterStatusBatcher(async id => row(id), many, () => new Error('missing'));
    const results = Promise.all([batch.get(1), batch.get(2)]);
    await vi.waitFor(() => expect(many).toHaveBeenCalled());
    batch.update(1, { progress: 90, state: 'PAUSE' });
    finish({ 1: { ...row(1, 12), current_archive_id: 44 }, 2: row(2) });
    expect((await results)[0]).toMatchObject({ progress: 90, state: 'PAUSE', current_archive_id: 44 });
  });

  it('fails a missing printer independently and does not cache between batches', async () => {
    const many = vi.fn(async () => ({ 1: row(1) }));
    const one = vi.fn(async id => row(id, 55));
    const batch = createPrinterStatusBatcher(one, many, () => new Error('missing'));
    const result = await Promise.allSettled([batch.get(1), batch.get(2)]);
    expect(result.map(item => item.status)).toEqual(['fulfilled', 'rejected']);
    expect((await batch.get(1)).progress).toBe(55);
    expect(one).toHaveBeenCalledTimes(1);
  });

  it('settles all waiters after a failed batch and can recover on the next read', async () => {
    const many = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ 1: row(1), 2: row(2) });
    const batch = createPrinterStatusBatcher(async id => row(id), many, () => new Error('missing'));
    expect((await Promise.allSettled([batch.get(1), batch.get(2)])).every(r => r.status === 'rejected')).toBe(true);
    expect(await Promise.all([batch.get(1), batch.get(2)])).toHaveLength(2);
  });

  it('splits larger fleets into sequential bounded requests', async () => {
    let active = 0;
    let maximum = 0;
    const many = vi.fn(async (ids: number[]) => {
      active += 1;
      maximum = Math.max(maximum, active);
      await new Promise(resolve => setTimeout(resolve, 1));
      active -= 1;
      return Object.fromEntries(ids.map(id => [id, row(id)]));
    });
    const batch = createPrinterStatusBatcher(async id => row(id), many, () => new Error('missing'));
    expect(await Promise.all(Array.from({ length: 250 }, (_, id) => batch.get(id)))).toHaveLength(250);
    expect(many.mock.calls.map(([ids]) => ids.length)).toEqual([100, 100, 50]);
    expect(maximum).toBe(1);
  });

  it('preserves measured Wi-Fi signal when later telemetry omits it', async () => {
    let finish!: (rows: Record<string, PrinterStatus>) => void;
    const many = vi.fn(() => new Promise<Record<string, PrinterStatus>>(resolve => { finish = resolve; }));
    const batch = createPrinterStatusBatcher(async id => row(id), many, () => new Error('missing'));
    const results = Promise.all([batch.get(1), batch.get(2)]);
    await vi.waitFor(() => expect(many).toHaveBeenCalled());
    batch.update(1, { wifi_signal: -45 });
    batch.update(1, { wifi_signal: null });
    batch.update(2, { wifi_signal: null });
    finish({ 1: { ...row(1), wifi_signal: -60 }, 2: { ...row(2), wifi_signal: -70 } });
    expect((await results).map(status => status.wifi_signal)).toEqual([-45, -70]);
  });
});
