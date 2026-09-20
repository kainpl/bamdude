import { describe, expect, it } from 'vitest';
import { monitorPrinter } from '../../../features/monitor/fixtures';
import { remainingSeconds, tileState } from '../../../features/monitor/state';
import { groupMonitor, sortMonitor } from '../../../features/monitor/sort';
import { readMonitorConfig, monitorUrl } from '../../../features/monitor/location';
import type { PrinterForecast } from '../../../api/client';

const config = readMonitorConfig(new URL('http://localhost/monitor'));
describe('monitor state and order contract', () => {
  it('does not show a leftover job timer after the printer becomes idle or terminal', () => {
    for (const state of ['IDLE', 'FINISH', 'FAILED']) {
      expect(remainingSeconds(monitorPrinter({ state }), '', Date.now(), false)).toBeNull();
    }
  });
  it('keeps physical printing and an independently paused queue', () => {
    const p = monitorPrinter(); p.queue!.is_paused = true;
    expect(tileState(p, 'printers').kind).toBe('printing');
    expect(tileState(p, 'queues')).toMatchObject({ kind: 'queuePaused', tone: 'warning', attention: true });
    p.queue!.is_paused = false; p.queue!.pending_count = 0;
    expect(tileState(p, 'queues')).toMatchObject({ kind: 'printing', tone: 'ok' });
  });
  it('distinguishes cancelled/terminal, clearance, pause and technical errors', () => {
    const p = monitorPrinter({ state: 'FAILED', awaiting_plate_clear: false });
    expect(tileState(p, 'printers').kind).toBe('stopped');
    p.awaiting_plate_clear = true;
    expect(tileState(p, 'printers').kind).toBe('plate');
    p.state = 'PAUSE';
    expect(tileState(p, 'printers').kind).toBe('paused');
    p.hms_errors = [{ code: '07008011', severity: 2 }];
    expect(tileState(p, 'printers').kind).toBe('error');
  });
  it('orders attention first, then 5/7/14 minutes, unknown time, idle, offline', () => {
    const rows = [
      monitorPrinter({ printer_id: 14, remaining_seconds: 840 }),
      monitorPrinter({ printer_id: 7, remaining_seconds: 420 }),
      monitorPrinter({ printer_id: 5, remaining_seconds: 300 }),
      monitorPrinter({ printer_id: 20, remaining_seconds: null }),
      monitorPrinter({ printer_id: 30, state: 'IDLE' }),
      monitorPrinter({ printer_id: 40, state: 'IDLE', connected: false, last_known_work_active: null }),
      monitorPrinter({ printer_id: 1, state: 'PAUSE' }),
    ];
    expect(sortMonitor(rows, config).map(p => p.printer_id)).toEqual([1, 5, 7, 14, 20, 30, 40]);
    expect(sortMonitor(rows, { ...config, sort: 'eta' }).slice(0, 3).map(p => p.printer_id)).toEqual([5, 7, 14]);
  });
  it('does not call missing forecast rows free and preserves partial estimates', () => {
    const a = monitorPrinter({ name: 'A', printer_id: 1 }), b = monitorPrinter({ name: 'B', printer_id: 2 });
    const partial = new Map<number, PrinterForecast>([[1, { printer_id: 1, free_seconds: 500, free_at: null, unknown_prints: 2 }]]);
    expect(sortMonitor([b, a], { ...config, sort: 'freeAt' }, partial).map(p => p.name)).toEqual(['A', 'B']);
    partial.set(2, { printer_id: 2, free_seconds: 0, free_at: null, unknown_prints: 0 });
    expect(sortMonitor([b, a], { ...config, sort: 'freeAt' }, partial).map(p => p.name)).toEqual(['A', 'B']);
  });
  it('preserves repeat membership in tags without inventing printers', () => {
    const p = monitorPrinter({ tags: ['A', 'B'] });
    const groups = groupMonitor([p], { ...config, group: 'tag' });
    expect(groups).toHaveLength(2);
    expect(new Set(groups.flatMap(g => g.printers.map(row => row.printer_id))).size).toBe(1);
    expect(groupMonitor([p], { ...config, group: 'tag', attention: true })).toHaveLength(1);
  });
  it('freezes paused/stale countdown and never makes completion a local state', () => {
    const generated = '2026-09-10T14:00:00Z', now = Date.parse(generated) + 600000;
    const p = monitorPrinter();
    expect(remainingSeconds(p, generated, now, false)).toBe(0);
    expect(tileState(p, 'printers').kind).toBe('printing');
    expect(remainingSeconds(p, generated, now, true)).toBe(300);
    p.state = 'PAUSE';
    expect(remainingSeconds(p, generated, now, false)).toBe(300);
  });
  it('only treats an offline printer as lost work when that is known', () => {
    expect(tileState(monitorPrinter({ connected: false, last_known_work_active: true }), 'printers').kind).toBe('lost');
    expect(tileState(monitorPrinter({ connected: false, last_known_work_active: null }), 'printers').kind).toBe('offline');
  });
  it('validates URL configuration and never puts a TV secret in query parameters', () => {
    const parsed = readMonitorConfig(new URL('http://localhost/monitor?sort=invalid&view=queues&size=0'), { sort: 'eta', size: '4' });
    expect(parsed.sort).toBe('attention'); expect(parsed.size).toBe('auto');
    const url = new URL(monitorUrl('queues', 'location', 'test-token'), 'http://localhost');
    expect(url.searchParams.has('token')).toBe(false); expect(url.hash).toBe('#token=test-token');
    expect(url.searchParams.get('group')).toBe('location');
  });
});
