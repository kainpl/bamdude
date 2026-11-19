/**
 * How long a completed print may stay unchecked before its one reminder
 * (upstream audit G04, part C): one global setting, 5 minutes to a week.
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
  outcome_reminder_after_minutes: 60,
};

async function openPrintingTab(overrides: Record<string, unknown> = {}) {
  server.use(http.get('/api/v1/settings/', () => HttpResponse.json({ ...mockSettings, ...overrides })));
  const user = userEvent.setup();
  render(<SettingsPage />);
  await waitFor(() => expect(screen.getAllByText('Printing').length).toBeGreaterThan(0));
  await user.click(screen.getAllByText('Printing')[0]);
  return await screen.findByLabelText('Unchecked result reminder', {}, { timeout: 5000 });
}

describe('Settings — unchecked result reminder', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/', () => HttpResponse.json([])),
      http.get('/api/v1/notifications/', () => HttpResponse.json([])),
      http.get('/api/v1/api-keys/', () => HttpResponse.json([])),
      http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: false, requires_setup: false })),
    );
  });

  it('shows the saved delay and says the reminder comes once', async () => {
    const input = await openPrintingTab({ outcome_reminder_after_minutes: 240 });
    expect(input).toHaveValue(240);
    expect(screen.getByText(/once per print/)).toBeInTheDocument();
  }, 20000);

  it('saves a changed delay, clamped to its range', async () => {
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

    fireEvent.change(input, { target: { value: '99999' } });
    fireEvent.blur(input);
    expect(input).toHaveValue(10080);
    await waitFor(() => expect(saved?.outcome_reminder_after_minutes).toBe(10080), { timeout: 5000 });

    fireEvent.change(input, { target: { value: '1' } });
    fireEvent.blur(input);
    expect(input).toHaveValue(5);
    await waitFor(() => expect(saved?.outcome_reminder_after_minutes).toBe(5), { timeout: 5000 });
  }, 20000);
});
