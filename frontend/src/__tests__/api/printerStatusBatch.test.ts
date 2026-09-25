import { describe, expect, it, vi } from 'vitest';
import type { PrinterStatus } from '../../api/client';
import { createPrinterStatusBatcher } from '../../api/printerStatusBatch';

const row = (id: number, progress = 1) => ({ id, progress, connected: true } as PrinterStatus);

describe('fleet status REST reads', () => {
  it('drops a cancelled waiter before flush without cancelling its neighbours', async () => {
    const one = vi.fn(async (id: number) => row(id));
    const many = vi.fn(async (ids: number[]) => Object.fromEntries(ids.map(id => [id, row(id)])));
    const batch = createPrinterStatusBatcher(one, many, () => new Error('missing'));
    const controller = new AbortController();
    const cancelled = batch.get(1, controller.signal);
    const others = Promise.all([batch.get(2), batch.get(3)]);
    controller.abort();
    await expect(cancelled).rejects.toMatchObject({ name: 'AbortError' });
    expect((await others).map(item => item.id)).toEqual([2, 3]);
    expect(many).toHaveBeenCalledTimes(1);
    expect(many.mock.calls[0][0]).toEqual([2, 3]);
  });

  it('does not abort shared transport for one cancelled waiter after send', async () => {
    let finish!: (rows: Record<string, PrinterStatus>) => void;
    let transportSignal: AbortSignal | undefined;
    const many = vi.fn((_ids: number[], signal?: AbortSignal) => {
      transportSignal = signal;
      return new Promise<Record<string, PrinterStatus>>(resolve => { finish = resolve; });
    });
    const batch = createPrinterStatusBatcher(async id => row(id), many, () => new Error('missing'));
    const controller = new AbortController();
    const cancelled = batch.get(1, controller.signal);
    const neighbour = batch.get(2);
    await vi.waitFor(() => expect(many).toHaveBeenCalledTimes(1));
    controller.abort();
    await expect(cancelled).rejects.toMatchObject({ name: 'AbortError' });
    expect(transportSignal?.aborted).toBe(false);
    finish({ 1: row(1), 2: row(2) });
    expect((await neighbour).id).toBe(2);
  });

  it('aborts transport when its final waiter cancels', async () => {
    let transportSignal: AbortSignal | undefined;
    const one = vi.fn((_id: number, signal?: AbortSignal) => {
      transportSignal = signal;
      return new Promise<PrinterStatus>((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(signal.reason), { once: true });
      });
    });
    const batch = createPrinterStatusBatcher(one, async () => ({}), () => new Error('missing'));
    const controller = new AbortController();
    const result = batch.get(1, controller.signal);
    await vi.waitFor(() => expect(one).toHaveBeenCalledTimes(1));
    controller.abort();
    await expect(result).rejects.toMatchObject({ name: 'AbortError' });
    expect(transportSignal?.aborted).toBe(true);
  });

  it('rejects an old-session result before it can enter the new session cache', async () => {
    let epoch = 1;
    let finish!: (rows: Record<string, PrinterStatus>) => void;
    const many = vi.fn(() => new Promise<Record<string, PrinterStatus>>(resolve => { finish = resolve; }));
    const batch = createPrinterStatusBatcher(async id => row(id), many, () => new Error('missing'), () => epoch);
    const old = Promise.allSettled([batch.get(1), batch.get(2)]);
    await vi.waitFor(() => expect(many).toHaveBeenCalledTimes(1));
    epoch = 2;
    finish({ 1: row(1), 2: row(2) });
    expect((await old).map(result => result.status)).toEqual(['rejected', 'rejected']);
  });

  it('turns 50 simultaneous reads into one request and keeps duplicates independent', async () => {
    const one = vi.fn(async (id: number) => row(id));
    const many = vi.fn(async (ids: number[]) => Object.fromEntries(ids.map(id => [id, row(id)])));
    const batch = createPrinterStatusBatcher(one, many, () => new Error('missing'));
    const result = await Promise.all([...Array.from({ length: 50 }, (_, id) => batch.get(id)), batch.get(3)]);
    expect(result.map(status => status.id)).toEqual([...Array.from({ length: 50 }, (_, id) => id), 3]);
    expect(many).toHaveBeenCalledTimes(1);
    expect(one).not.toHaveBeenCalled();
  });

  it('applies one response to card observers in small tasks without splitting the HTTP request', async () => {
    vi.useFakeTimers();
    try {
      const one = vi.fn(async (id: number) => row(id));
      const many = vi.fn(async (ids: number[]) => Object.fromEntries(ids.map(id => [id, row(id)])));
      const settled = vi.fn();
      const batch = createPrinterStatusBatcher(one, many, () => new Error('missing'));
      const results = Array.from({ length: 25 }, (_, id) => batch.get(id).then(settled));

      await vi.advanceTimersToNextTimerAsync();
      expect(many).toHaveBeenCalledTimes(1);
      // Vitest may run one nested 0ms yield in the same advancement, but it
      // must not have drained the whole 25-card response in that turn.
      expect(settled.mock.calls.length).toBeGreaterThanOrEqual(10);
      expect(settled.mock.calls.length).toBeLessThan(25);

      await vi.runAllTimersAsync();
      await Promise.all(results);
      expect(settled).toHaveBeenCalledTimes(25);
    } finally {
      vi.useRealTimers();
    }
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
