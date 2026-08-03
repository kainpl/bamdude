/**
 * The sensor alert toggles, on the REAL card.
 *
 * A separate file because `NotificationProviderCard.test.tsx` mocks the very
 * component it is named after, so every assertion in it is about the mock.
 * Adding these there would have been vacuous by construction.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { NotificationProviderCard } from '../../components/NotificationProviderCard';
import { api } from '../../api/client';
import type { NotificationProvider } from '../../api/client';

function provider(over: Partial<NotificationProvider> = {}): NotificationProvider {
  return {
    id: 1,
    name: 'ntfy',
    provider_type: 'ntfy',
    enabled: true,
    config: { topic: 'bamdude' },
    on_print_start: false,
    on_print_complete: true,
    on_print_failed: true,
    on_print_stopped: true,
    on_print_progress: false,
    on_print_missing_spool_assignment: false,
    on_printer_offline: false,
    on_printer_error: false,
    on_ai_failure_detection: false,
    on_filament_low: false,
    on_maintenance_due: false,
    on_ams_humidity_high: false,
    on_ams_temperature_high: false,
    on_ams_ht_humidity_high: false,
    on_ams_ht_temperature_high: false,
    on_sensor_threshold: false,
    on_sensor_silent: false,
    on_plate_not_empty: true,
    on_bed_cooled: false,
    on_first_layer_complete: false,
    on_queue_job_added: false,
    on_queue_job_started: false,
    on_queue_job_waiting: true,
    on_queue_job_skipped: true,
    on_queue_job_failed: true,
    on_queue_completed: false,
    on_printer_queue_completed: false,
    on_stock_reorder_alert: false,
    on_stock_break_alert: false,
    quiet_hours_enabled: false,
    quiet_hours_start: null,
    quiet_hours_end: null,
    daily_digest_enabled: false,
    daily_digest_time: null,
    printer_id: null,
    last_success: null,
    last_error: null,
    last_error_at: null,
    created_at: '2026-08-04T10:00:00Z',
    updated_at: '2026-08-04T10:00:00Z',
  } as NotificationProvider;
}

async function openEventSettings() {
  await userEvent.click(await screen.findByRole('button', { name: /Event Settings/i }));
}

describe('NotificationProviderCard — sensor alerts', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('offers both toggles, and only two', async () => {
    // Two toggles against five messages: the raise and its all-clear are never
    // divided, but "the room" and "the device" are.
    render(<NotificationProviderCard provider={provider()} onEdit={() => {}} />);
    await openEventSettings();

    expect(screen.getByText('Sensor readings')).toBeInTheDocument();
    expect(screen.getByText('Sensor went silent')).toBeInTheDocument();
    expect(screen.queryByText('Reading back in range')).not.toBeInTheDocument();
  });

  it('saves the threshold toggle under its own field', async () => {
    // The two must not be wired to the same field: turning one on would then
    // silently turn on the other, and the card would look right.
    const update = vi.spyOn(api, 'updateNotificationProvider').mockResolvedValue(provider());

    render(<NotificationProviderCard provider={provider()} onEdit={() => {}} />);
    await openEventSettings();
    await userEvent.click(screen.getByRole('switch', { name: 'Sensor readings' }));

    expect(update).toHaveBeenCalledWith(1, { on_sensor_threshold: true });
  });

  it('saves the silence toggle under its own field', async () => {
    const update = vi.spyOn(api, 'updateNotificationProvider').mockResolvedValue(provider());

    render(<NotificationProviderCard provider={provider()} onEdit={() => {}} />);
    await openEventSettings();
    await userEvent.click(screen.getByRole('switch', { name: 'Sensor went silent' }));

    expect(update).toHaveBeenCalledWith(1, { on_sensor_silent: true });
  });
});
