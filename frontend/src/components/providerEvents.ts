import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import type { ProviderEventInfo } from '../api/client';

/**
 * The event list a notification provider subscribes from, and its wording.
 *
 * Separate from the component so the file that renders it exports a component
 * and nothing else (react-refresh). `ProviderEventToggles` is the renderer.
 *
 * ⚠️ The LIST is never written here. `GET /notifications/events` returns one row
 * per flag in `PROVIDER_EVENT_DEFAULTS`, grouped as the notification centre
 * groups them, and this component renders whatever arrives. That is the whole
 * point: the dialog and the card each used to keep their own list, and they
 * drifted — the dialog offered 18 of the 34 flags, and six of the sixteen it
 * hid default to ON, so a new provider sent events nobody had been shown.
 *
 * Only the WORDING is local, in three tiers, each a fallback for the one above:
 * the card's own purpose-written key (richest — many carry a description), then
 * the notification centre's per-event key (present for every catalogued event
 * in both locales), then the flag itself, humanised. So a flag this file has
 * never heard of still renders, with a readable label — it can never be
 * silently dropped from the form, which is the failure this component exists
 * to make impossible.
 */

/** Flag → the card's own label key. Presentation only; absence is not an error. */
const LABEL_KEYS: Record<string, string> = {
  on_print_start: 'printStarted',
  on_plate_not_empty: 'plateNotEmpty',
  on_print_complete: 'printCompleted',
  on_bed_cooled: 'bedCooledLabel',
  on_first_layer_complete: 'firstLayerCompleteLabel',
  on_print_missing_spool_assignment: 'missingSpoolAssignmentLabel',
  on_print_failed: 'printFailed',
  on_print_stopped: 'printStopped',
  on_print_paused: 'printPausedLabel',
  on_print_resumed: 'printResumedLabel',
  on_print_progress: 'progressMilestones',
  on_printer_offline: 'printerOffline',
  on_printer_error: 'printerError',
  on_ai_failure_detection: 'aiFailureDetection',
  on_filament_low: 'lowFilamentLabel',
  on_filament_runout: 'filamentRunout',
  on_filament_deficit: 'filamentDeficit',
  on_maintenance_due: 'maintenanceDue',
  on_ams_humidity_high: 'amsHumidityHigh',
  on_ams_temperature_high: 'amsTemperatureHigh',
  on_ams_drying_suspended: 'amsDryingSuspended',
  on_ams_ht_humidity_high: 'amsHtHumidityHigh',
  on_ams_ht_temperature_high: 'amsHtTemperatureHigh',
  on_sensor_threshold: 'sensorThreshold',
  on_sensor_silent: 'sensorSilent',
  on_stock_reorder_alert: 'stockReorderAlert',
  on_stock_break_alert: 'stockBreakAlert',
  on_queue_job_added: 'jobAdded',
  on_queue_job_started: 'jobStarted',
  on_queue_job_waiting: 'jobWaiting',
  on_queue_job_skipped: 'jobSkipped',
  on_queue_job_failed: 'jobFailed',
  on_queue_completed: 'queueComplete',
  on_printer_queue_completed: 'printerQueueComplete',
};

/** Flag → the card's description key, for the flags that have one. */
export const DESCRIPTION_KEYS: Record<string, string> = {
  on_plate_not_empty: 'plateNotEmptyDescription',
  on_bed_cooled: 'bedCooledDescription',
  on_first_layer_complete: 'firstLayerCompleteDescription',
  on_print_missing_spool_assignment: 'missingSpoolAssignmentDescription',
  on_print_paused: 'printPausedDescription',
  on_print_resumed: 'printResumedDescription',
  on_print_progress: 'progressMilestonesDescription',
  on_ai_failure_detection: 'aiFailureDetectionDescription',
  on_maintenance_due: 'maintenanceDueDescription',
  on_ams_humidity_high: 'amsHumidityHighDescription',
  on_ams_temperature_high: 'amsTemperatureHighDescription',
  on_ams_drying_suspended: 'amsDryingSuspendedDescription',
  on_ams_ht_humidity_high: 'amsHtHumidityHighDescription',
  on_ams_ht_temperature_high: 'amsHtTemperatureHighDescription',
  on_sensor_threshold: 'sensorThresholdDescription',
  on_sensor_silent: 'sensorSilentDescription',
  on_stock_reorder_alert: 'stockReorderAlertDescription',
  on_stock_break_alert: 'stockBreakAlertDescription',
  on_queue_job_added: 'jobAddedDescription',
  on_queue_job_started: 'jobStartedDescription',
  on_queue_job_waiting: 'jobWaitingDescription',
  on_queue_job_skipped: 'jobSkippedDescription',
  on_queue_job_failed: 'jobFailedDescription',
  on_queue_completed: 'queueCompleteDescription',
  on_printer_queue_completed: 'printerQueueCompleteDescription',
};

/** "on_queue_job_waiting" → "Queue job waiting". Last resort, never blank. */
export function humaniseFlag(flag: string): string {
  const words = flag.replace(/^on_/, '').split('_');
  if (words.length === 0 || words[0] === '') return flag;
  return [words[0].charAt(0).toUpperCase() + words[0].slice(1), ...words.slice(1)].join(' ');
}

/** Shared by the dialog and the card so one list feeds the toggles and the chips. */
export function useProviderEvents() {
  return useQuery({
    queryKey: ['notification-events'],
    queryFn: api.getNotificationEvents,
    // The list changes with a release, never during a session.
    staleTime: Infinity,
  });
}

/** The label a flag shows, resolved through the three tiers described above. */
export function useEventLabel() {
  const { t } = useTranslation();
  return (event: ProviderEventInfo) => {
    const own = LABEL_KEYS[event.flag];
    if (own) return t(`notifications.${own}`);
    const catalogued = event.event_types[0];
    if (catalogued) {
      return t(`notifications.center.events.${catalogued}`, { defaultValue: humaniseFlag(event.flag) });
    }
    return humaniseFlag(event.flag);
  };
}
