/**
 * Auto-drying waits for humidity that STAYS above the threshold (upstream
 * #2518): a switch (on = 15 minutes, off = 0) and a minutes field.
 *
 * The wait belongs to auto-drying as a whole — it also covers a mid-print
 * start with ambient drying off — so the control is NOT nested under the
 * ambient switch.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { SettingsPage } from '../../pages/SettingsPage';
import { server } from '../mocks/server';

const mockSettings = {
  save_thumbnails: true,
  capture_finish_photo: true,
  default_filament_cost: 25.0,
  currency: 'USD',
  ams_humidity_good: 40,
  ams_humidity_fair: 60,
  ams_temp_good: 30,
  ams_temp_fair: 35,
  ams_temp_alarm: null,
  time_format: 'system',
  date_format: 'system',
  mqtt_enabled: false,
  spoolman_enabled: false,
  ha_enabled: false,
  check_updates: false,
  check_printer_firmware: false,
  bed_cooled_threshold: 35,
  queue_drying_enabled: false,
  ambient_drying_enabled: false,
  print_drying_enabled: false,
  auto_drying_sustained_minutes: 15,
};

let saved: Record<string, unknown> | null = null;

async function openFilamentTabWith(overrides: Record<string, unknown>) {
  server.use(http.get('/api/v1/settings/', () => HttpResponse.json({ ...mockSettings, ...overrides })));
  render(<SettingsPage />);
  await waitFor(() => expect(screen.getAllByText('Filament').length).toBeGreaterThan(0));
  await userEvent.click(screen.getAllByText('Filament')[0]);
  await waitFor(() => expect(screen.getByText('Wait for sustained humidity')).toBeInTheDocument());
  // The page holds back auto-save briefly after the settings load.
  await new Promise((resolve) => setTimeout(resolve, 200));
}

const sustainedSwitch = () =>
  within(screen.getByText('Wait for sustained humidity').closest('div')!.parentElement!).getByRole('checkbox');
const minutesInput = () => screen.queryByLabelText('Sustained time (minutes)') as HTMLInputElement | null;

describe('auto-drying sustained-humidity wait', () => {
  beforeEach(() => {
    saved = null;
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/', () => HttpResponse.json([])),
      http.get('/api/v1/notifications/', () => HttpResponse.json([])),
      http.get('/api/v1/api-keys/', () => HttpResponse.json([])),
      http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: false, requires_setup: false })),
      http.put('/api/v1/settings/', async ({ request }) => {
        saved = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...mockSettings, ...saved });
      }),
    );
  });

  it('is shown with ambient drying off, switched on, with its minutes', async () => {
    await openFilamentTabWith({ ambient_drying_enabled: false });

    expect(sustainedSwitch()).toBeChecked();
    expect(minutesInput()).toHaveValue(15);
  });

  it('switching it off saves 0 and hides the minutes', async () => {
    await openFilamentTabWith({});

    await userEvent.click(sustainedSwitch());

    await waitFor(() => expect(saved?.auto_drying_sustained_minutes).toBe(0), { timeout: 3000 });
    expect(minutesInput()).toBeNull();
  });

  it('switching it on saves 15', async () => {
    await openFilamentTabWith({ auto_drying_sustained_minutes: 0 });
    expect(sustainedSwitch()).not.toBeChecked();
    expect(minutesInput()).toBeNull();

    await userEvent.click(sustainedSwitch());

    await waitFor(() => expect(saved?.auto_drying_sustained_minutes).toBe(15), { timeout: 3000 });
    expect(minutesInput()).toHaveValue(15);
  });

  it('clamps the minutes to 5..240 when the field is left, not while typing', async () => {
    await openFilamentTabWith({});
    const input = minutesInput()!;

    await userEvent.clear(input);
    await userEvent.type(input, '2');
    expect(input).toHaveValue(2);
    await userEvent.tab();
    await waitFor(() => expect(saved?.auto_drying_sustained_minutes).toBe(5), { timeout: 3000 });
    expect(minutesInput()).toHaveValue(5);

    await userEvent.clear(minutesInput()!);
    await userEvent.type(minutesInput()!, '999');
    await userEvent.tab();
    await waitFor(() => expect(saved?.auto_drying_sustained_minutes).toBe(240), { timeout: 3000 });
  });

  it('an emptied field goes back to the saved minutes', async () => {
    await openFilamentTabWith({ auto_drying_sustained_minutes: 30 });

    await userEvent.clear(minutesInput()!);
    await userEvent.tab();

    expect(minutesInput()).toHaveValue(30);
  });
});
