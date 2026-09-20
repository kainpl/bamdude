import type { MonitorPrinter, MonitorSnapshot } from './types';

/** Contract fixtures for tests and design review; never imported by the live entry. */
export function monitorPrinter(overrides: Partial<MonitorPrinter> = {}): MonitorPrinter {
  return {
    printer_id: 1, name: 'P1S-01', model: 'P1S', location: 'Workshop A', tags: [], is_active: true,
    connected: true, state: 'RUNNING', derived_stage: null, progress: 80, remaining_seconds: 300,
    layer_num: 200, total_layers: 250, temperatures: { bed: 60, nozzle: 220 },
    status_received_at: '2026-09-10T14:00:00Z', source_stale: false, last_known_work_active: true,
    hms_errors: [], pause_reason: null, pause_started_at: null, require_plate_clear: true, awaiting_plate_clear: false,
    current_job: { visibility: 'visible', name: 'Bracket.3mf', item_id: 1 }, dispatch: null,
    queue: { queue_id: 1, status: 'printing', is_paused: false, auto_distribute_eligible: true, pending_count: 2,
      next_job: { visibility: 'visible', name: 'Mount.3mf', item_id: 2 }, waiting: null },
    ...overrides,
  };
}

export function monitorSnapshot(printers = [monitorPrinter()]): MonitorSnapshot {
  return { generated_at: '2026-09-10T14:00:00Z', view: 'printers', printers,
    capabilities: { queues: true, forecast: true, job_details: true, open_printer: true, open_queue: true } };
}
