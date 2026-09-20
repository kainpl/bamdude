export type MonitorView = 'printers' | 'queues';
export type MonitorSort = 'attention' | 'eta' | 'freeAt' | 'name';
export type MonitorGroup = 'none' | 'location' | 'tag';
export type MonitorSize = 'auto' | '1' | '2' | '3' | '4';
export type WaitCode = 'printer_offline' | 'plate_not_cleared' | 'drying' | 'scheduled' |
  'manual_start' | 'stagger' | 'filament_unavailable' | 'dispatch_failed' | 'storage_full' | 'unknown';
export type DispatchPhase = 'preparing' | 'uploading' | 'heating' | 'swapping' | 'starting' | 'acknowledging';
export type MonitorJob = { visibility: 'restricted' } | { visibility: 'visible'; name: string | null; item_id: number | null };
export interface MonitorPrinter {
  printer_id: number;
  name: string;
  model: string | null;
  location: string | null;
  tags: string[];
  is_active: boolean;
  connected: boolean;
  state: string | null;
  derived_stage: 'preparing' | 'heating' | 'swapping' | null;
  progress: number | null;
  remaining_seconds: number | null;
  layer_num: number | null;
  total_layers: number | null;
  temperatures: Record<string, number>;
  status_received_at: string | null;
  source_stale: boolean;
  last_known_work_active: boolean | null;
  hms_errors: { code: string; severity: number }[];
  pause_reason: string | null;
  pause_started_at: string | null;
  require_plate_clear: boolean;
  awaiting_plate_clear: boolean;
  current_job: MonitorJob | null;
  queue: {
    queue_id: number; status: string; is_paused: boolean; auto_distribute_eligible: boolean;
    pending_count: number; next_job: MonitorJob | null;
    waiting: { code: WaitCode; since: string | null; until: string | null } | null;
  } | null;
  dispatch: { phase: DispatchPhase; upload_progress: number | null } | null;
}
export interface MonitorSnapshot {
  generated_at: string;
  view: MonitorView;
  printers: MonitorPrinter[];
  capabilities: { queues: boolean; forecast: boolean; job_details: boolean; open_printer: boolean; open_queue: boolean };
}
export interface MonitorConfig {
  view: MonitorView; sort: MonitorSort; group: MonitorGroup; size: MonitorSize;
  color: 'strong' | 'soft'; lang: 'uk' | 'en'; search: string; attention: boolean;
}
