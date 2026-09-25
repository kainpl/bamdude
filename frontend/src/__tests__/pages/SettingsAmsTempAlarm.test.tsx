/**
 * The AMS temperature alarm has its own threshold, separate from the Fair
 * display band (upstream #2905). Unset means "alarm at Fair", not "never
 * alarm" — everything below turns on telling those two apart.
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
};

async function openFilamentTabWith(overrides: Record<string, unknown>) {
  server.use(http.get('/api/v1/settings/', () => HttpResponse.json({ ...mockSettings, ...overrides })));
  const user = userEvent.setup();
  render(<SettingsPage />);
  await waitFor(() => expect(screen.getAllByText('Filament').length).toBeGreaterThan(0));
  await user.click(screen.getAllByText('Filament')[0]);
  await waitFor(() => expect(screen.getByText('AMS Display Thresholds')).toBeInTheDocument());
}

const alarmInput = () => within(screen.getByText('Alarm above').parentElement!).getByRole('spinbutton');

describe('AMS temperature alarm threshold', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/', () => HttpResponse.json([])),
      http.get('/api/v1/notifications/', () => HttpResponse.json([])),
      http.get('/api/v1/api-keys/', () => HttpResponse.json([])),
      http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: false, requires_setup: false })),
    );
  });

  it('shows the fair threshold as the placeholder while unset', async () => {
    // Blank with no hint would read as "no alarm" — the opposite of what unset does.
    await openFilamentTabWith({ ams_temp_fair: 38, ams_temp_alarm: null });

    const input = alarmInput();
    expect(input).toHaveValue(null);
    expect(input).toHaveAttribute('placeholder', '38');
  });

  it('sends the typed threshold on save', async () => {
    let saved: Record<string, unknown> | null = null;
    server.use(
      http.put('/api/v1/settings/', async ({ request }) => {
        saved = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...mockSettings, ...saved });
      }),
    );
    await openFilamentTabWith({ ams_temp_alarm: null });
    // The page holds back auto-save briefly after the settings load.
    await new Promise((resolve) => setTimeout(resolve, 200));

    await userEvent.type(alarmInput(), '45');

    await waitFor(() => expect(saved?.ams_temp_alarm).toBe(45), { timeout: 3000 });
  });

  it('sends an explicit null when the field is cleared', async () => {
    // Omitting the key would keep the old threshold; the backend needs the null.
    let saved: Record<string, unknown> | null = null;
    server.use(
      http.put('/api/v1/settings/', async ({ request }) => {
        saved = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...mockSettings, ...saved });
      }),
    );
    await openFilamentTabWith({ ams_temp_alarm: 45 });
    await new Promise((resolve) => setTimeout(resolve, 200));

    await userEvent.clear(alarmInput());

    await waitFor(
      () => {
        expect(saved).not.toBeNull();
        expect(saved!.ams_temp_alarm).toBeNull();
      },
      { timeout: 3000 },
    );
  });

  it('warns that a non-positive threshold is ignored', async () => {
    await openFilamentTabWith({ ams_temp_alarm: 0 });

    expect(await screen.findByText(/A threshold of 0 or less is ignored/)).toBeInTheDocument();
  });

  it('stays quiet for a threshold the backend will honour', async () => {
    await openFilamentTabWith({ ams_temp_alarm: 45 });

    expect(screen.queryByText(/A threshold of 0 or less is ignored/)).not.toBeInTheDocument();
  });
});
