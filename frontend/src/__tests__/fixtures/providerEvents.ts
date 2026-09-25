import type { ProviderEventInfo } from '../../api/client';

/**
 * What `GET /notifications/events` returns, captured from the real endpoint.
 *
 * Hand-kept, deliberately: this is test DATA, not a source the UI reads. Its
 * completeness is pinned on the backend
 * (`TestProviderEventsEndpoint::test_every_provider_flag_is_listed_exactly_once`),
 * so a flag added to `PROVIDER_EVENT_DEFAULTS` and forgotten here fails there,
 * not silently here. Tests that care about an unknown flag append their own row
 * rather than editing this one.
 */
export const PROVIDER_EVENTS: ProviderEventInfo[] = [
  { flag: 'on_print_failed', event_types: ['print_failed'], group: 'print', severity: 'error', default: true },
  { flag: 'on_print_missing_spool_assignment', event_types: ['print_missing_spool_assignment'], group: 'print', severity: 'warning', default: false },
  { flag: 'on_print_paused', event_types: ['print_paused'], group: 'print', severity: 'warning', default: true },
  { flag: 'on_print_stopped', event_types: ['print_stopped'], group: 'print', severity: 'warning', default: true },
  { flag: 'on_bed_cooled', event_types: ['bed_cooled'], group: 'print', severity: 'info', default: false },
  { flag: 'on_first_layer_complete', event_types: ['first_layer_complete'], group: 'print', severity: 'info', default: false },
  { flag: 'on_print_complete', event_types: ['print_complete'], group: 'print', severity: 'info', default: true },
  { flag: 'on_print_progress', event_types: ['print_progress'], group: 'print', severity: 'info', default: false },
  { flag: 'on_print_resumed', event_types: ['print_resumed'], group: 'print', severity: 'info', default: true },
  { flag: 'on_print_start', event_types: ['print_start'], group: 'print', severity: 'info', default: false },
  { flag: 'on_ai_failure_detection', event_types: ['ai_failure_detection'], group: 'printer', severity: 'error', default: false },
  { flag: 'on_printer_error', event_types: ['printer_error'], group: 'printer', severity: 'error', default: false },
  { flag: 'on_printer_offline', event_types: ['printer_offline'], group: 'printer', severity: 'error', default: false },
  { flag: 'on_maintenance_due', event_types: ['maintenance_due'], group: 'printer', severity: 'warning', default: false },
  { flag: 'on_plate_not_empty', event_types: ['plate_not_empty'], group: 'printer', severity: 'warning', default: true },
  { flag: 'on_filament_runout', event_types: ['filament_runout'], group: 'filament', severity: 'error', default: false },
  { flag: 'on_filament_deficit', event_types: ['filament_deficit'], group: 'filament', severity: 'warning', default: true },
  { flag: 'on_filament_low', event_types: ['filament_low'], group: 'filament', severity: 'warning', default: false },
  { flag: 'on_ams_ht_humidity_high', event_types: ['ams_ht_humidity_high'], group: 'ams', severity: 'error', default: false },
  { flag: 'on_ams_ht_temperature_high', event_types: ['ams_ht_temperature_high'], group: 'ams', severity: 'error', default: false },
  { flag: 'on_ams_humidity_high', event_types: ['ams_humidity_high'], group: 'ams', severity: 'error', default: false },
  { flag: 'on_ams_temperature_high', event_types: ['ams_temperature_high'], group: 'ams', severity: 'error', default: false },
  { flag: 'on_ams_drying_suspended', event_types: ['ams_drying_suspended'], group: 'ams', severity: 'warning', default: true },
  { flag: 'on_scheduled_drying_failed', event_types: ['scheduled_drying_failed'], group: 'ams', severity: 'warning', default: true },
  { flag: 'on_scheduled_drying_completed', event_types: ['scheduled_drying_completed'], group: 'ams', severity: 'info', default: false },
  { flag: 'on_scheduled_drying_started', event_types: ['scheduled_drying_started'], group: 'ams', severity: 'info', default: false },
  { flag: 'on_queue_job_failed', event_types: ['queue_job_failed'], group: 'queue', severity: 'error', default: true },
  { flag: 'on_queue_job_skipped', event_types: ['queue_job_skipped'], group: 'queue', severity: 'warning', default: true },
  { flag: 'on_queue_job_waiting', event_types: ['queue_job_waiting'], group: 'queue', severity: 'warning', default: true },
  { flag: 'on_printer_queue_completed', event_types: ['printer_queue_completed'], group: 'queue', severity: 'info', default: true },
  { flag: 'on_queue_completed', event_types: ['queue_completed'], group: 'queue', severity: 'info', default: false },
  { flag: 'on_queue_job_added', event_types: ['queue_job_added'], group: 'queue', severity: 'info', default: false },
  { flag: 'on_queue_job_started', event_types: ['queue_job_started'], group: 'queue', severity: 'info', default: false },
  { flag: 'on_stock_break_alert', event_types: ['stock_break_alert'], group: 'inventory', severity: 'error', default: false },
  { flag: 'on_stock_reorder_alert', event_types: ['stock_reorder_alert'], group: 'inventory', severity: 'warning', default: false },
  { flag: 'on_sensor_threshold', event_types: ['sensor_above_max', 'sensor_below_min', 'sensor_back_in_range'], group: 'sensors', severity: 'error', default: false },
  { flag: 'on_sensor_silent', event_types: ['sensor_silent', 'sensor_speaking_again'], group: 'sensors', severity: 'warning', default: false },
];
