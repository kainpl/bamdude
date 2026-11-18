/**
 * The lead time of the “Pause coming up” event (upstream 8901ff849, adapted):
 * one global setting beside the other notification timing, 0 = the event is off.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
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
  time_format: 'system',
  date_format: 'system',
  mqtt_enabled: false,
  spoolman_enabled: false,
  ha_enabled: false,
  check_updates: false,
  check_printer_firmware: false,
  bed_cooled_threshold: 35,
  pause_warning_minutes: 5,
};

async function openPrintingTab(overrides: Record<string, unknown> = {}) {
  server.use(http.get('/api/v1/settings/', () => HttpResponse.json({ ...mockSettings, ...overrides })));
  const user = userEvent.setup();
  render(<SettingsPage />);
  await waitFor(() => expect(screen.getAllByText('Printing').length).toBeGreaterThan(0));
  await user.click(screen.getAllByText('Printing')[0]);
  return await screen.findByLabelText('Pause heads-up lead time', {}, { timeout: 5000 });
}

describe('Settings — pause heads-up lead time', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/', () => HttpResponse.json([])),
      http.get('/api/v1/notifications/', () => HttpResponse.json([])),
      http.get('/api/v1/api-keys/', () => HttpResponse.json([])),
      http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: false, requires_setup: false })),
    );
  });

  it('shows the saved lead time and says what it does, including 0', async () => {
    const input = await openPrintingTab({ pause_warning_minutes: 12 });
    expect(input).toHaveValue(12);
    expect(screen.getByText(/layer before the pause/)).toBeInTheDocument();
    expect(screen.getByText(/0 switches the event off/)).toBeInTheDocument();
  }, 20000);

  it('saves a changed lead time, clamped to its range', async () => {
    let saved: Record<string, unknown> | null = null;
    server.use(
      http.put('/api/v1/settings/', async ({ request }) => {
        saved = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...mockSettings, ...saved });
      }),
    );
    const input = await openPrintingTab();
    // The page holds back auto-save briefly after the settings load.
    await new Promise((resolve) => setTimeout(resolve, 200));

    // An out-of-range number stays as typed and is clamped when the field is left.
    fireEvent.change(input, { target: { value: '500' } });
    expect(input).toHaveValue(500);
    fireEvent.blur(input);
    expect(input).toHaveValue(120);
    await waitFor(() => expect(saved?.pause_warning_minutes).toBe(120), { timeout: 5000 });

    fireEvent.change(input, { target: { value: '0' } });
    await waitFor(() => expect(saved?.pause_warning_minutes).toBe(0), { timeout: 5000 });
  }, 20000);
});
