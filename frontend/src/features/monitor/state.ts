import type { MonitorPrinter, MonitorView } from './types';

export type StateKind = 'error' | 'warning' | 'paused' | 'plate' | 'queuePaused' | 'queueError' |
  'filament' | 'manual' | 'dispatchFailed' | 'storageFull' | 'lost' | 'printing' | 'preparing' |
  'heating' | 'uploading' | 'swapping' | 'scheduled' | 'stagger' | 'drying' | 'waiting' |
  'finished' | 'stopped' | 'idle' | 'maintenance' | 'offline' | 'unknown';
export type Tone = 'ok' | 'warning' | 'error' | 'info' | 'neutral';
export interface TileState { kind: StateKind; tone: Tone; rank: number; attention: boolean; since: number }

export function tileState(p: MonitorPrinter, view: MonitorView): TileState {
  const state = (kind: StateKind, tone: Tone, rank: number, attention = false): TileState => ({
    kind, tone, rank, attention,
    since: Date.parse(p.pause_started_at || p.queue?.waiting?.since || '') || Infinity,
  });
  // Retain a signal of lost work, without declaring every disconnected idle
  // machine an incident or losing the last known state to HTTP failure.
  if (!p.connected) return p.last_known_work_active ? state('lost', 'warning', 3, true) : state('offline', 'neutral', 9);
  if (p.hms_errors.some(e => e.severity <= 2)) return state('error', 'error', 0, true);
  if (p.state === 'PAUSE') return state('paused', 'warning', 1, true);
  if (p.hms_errors.length) return state('warning', 'warning', 1, true);
  if (p.require_plate_clear && p.awaiting_plate_clear && p.state !== 'RUNNING') return state('plate', 'info', 2, true);
  if (view === 'queues') {
    if (p.queue?.is_paused || p.queue?.status === 'paused') return state('queuePaused', 'warning', 3, true);
    if (p.queue?.status === 'error') return state('queueError', 'warning', 3, true);
    const wait = p.queue?.waiting?.code;
    if (wait === 'filament_unavailable') return state('filament', 'warning', 3, true);
    if (wait === 'manual_start') return state('manual', 'warning', 3, true);
    if (wait === 'dispatch_failed') return state('dispatchFailed', 'warning', 3, true);
    if (wait === 'storage_full') return state('storageFull', 'warning', 3, true);
  }
  if (p.dispatch) {
    const phase = p.dispatch.phase;
    return state(phase === 'starting' || phase === 'acknowledging' ? 'preparing' : phase,
      phase === 'swapping' ? 'info' : 'ok', 6);
  }
  if (p.state === 'RUNNING') {
    if (p.derived_stage) return state(p.derived_stage, p.derived_stage === 'swapping' ? 'info' : 'ok', 6);
    return state('printing', 'ok', p.remaining_seconds != null && p.remaining_seconds > 0 ? 4 : 5);
  }
  if (p.state === 'PREPARE' || p.state === 'SLICING') return state('preparing', 'ok', 6);
  const wait = view === 'queues' ? p.queue?.waiting?.code : null;
  if (wait === 'scheduled' || wait === 'stagger' || wait === 'drying') return state(wait, 'info', 6);
  if (wait) return state('waiting', 'info', 6);
  if (!p.is_active) return state('maintenance', 'neutral', 8);
  if (p.state === 'FINISH') return state('finished', 'neutral', 7);
  if (p.state === 'FAILED') return state('stopped', 'neutral', 7);
  if (!p.state || p.state === 'unknown') return state('unknown', 'neutral', 7);
  return state('idle', 'neutral', 7);
}

export function remainingSeconds(p: MonitorPrinter, generatedAt: string, now: number, stale: boolean): number | null {
  if (!['RUNNING', 'PAUSE', 'PREPARE', 'SLICING'].includes(p.state || '')) return null;
  if (p.remaining_seconds == null) return null;
  if (stale || p.source_stale || p.state !== 'RUNNING') return p.remaining_seconds;
  const age = Math.max(0, (now - Date.parse(generatedAt)) / 1000);
  return Math.max(0, p.remaining_seconds - age);
}
