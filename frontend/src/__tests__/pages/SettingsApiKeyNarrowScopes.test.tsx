/**
 * The two narrow API key opt-ins can be set where keys are made: the electricity-price door (it existed,
 * but no toggle did — docs told people to flip one that was not there) and external app messages
 * (spec vault 60-specs/external-app-messages-spec). Both start off.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { SettingsPage } from '../../pages/SettingsPage';
import { server } from '../mocks/server';

describe('Settings — narrow API key opt-ins', () => {
  let created: Record<string, unknown> | null;

  beforeEach(() => {
    created = null;
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'USD', check_updates: false, check_printer_firmware: false })),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/', () => HttpResponse.json([])),
      http.get('/api/v1/notifications/', () => HttpResponse.json([])),
      http.get('/api/v1/api-keys/', () => HttpResponse.json([])),
      http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: false, requires_setup: false })),
      http.post('/api/v1/api-keys/', async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: 1, name: 'HA', key: 'bd_x', key_prefix: 'bd_x', ...created });
      }),
    );
  });

  async function openCreateForm() {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await waitFor(() => expect(screen.getAllByText('API Keys').length).toBeGreaterThan(0));
    await user.click(screen.getAllByText('API Keys')[0]);
    await user.click(await screen.findByRole('button', { name: /create key/i }, { timeout: 5000 }));
    return user;
  }

  it('offers both and sends them only when ticked', async () => {
    const user = await openCreateForm();
    const energy = await screen.findByRole('checkbox', { name: /update the electricity price/i });
    const messages = screen.getByRole('checkbox', { name: /send notifications as an external app/i });
    expect(energy).not.toBeChecked();
    expect(messages).not.toBeChecked();

    await user.click(energy);
    await user.click(messages);
    const submit = screen.getAllByRole('button', { name: /create key/i });
    await user.click(submit[submit.length - 1]);

    await waitFor(() => expect(created).not.toBeNull());
    expect(created).toMatchObject({ can_update_energy_cost: true, can_send_notifications: true });
  }, 20000);

  it('leaves both off when untouched', async () => {
    const user = await openCreateForm();
    await screen.findByRole('checkbox', { name: /send notifications as an external app/i });
    const submit = screen.getAllByRole('button', { name: /create key/i });
    await user.click(submit[submit.length - 1]);

    await waitFor(() => expect(created).not.toBeNull());
    expect(created).toMatchObject({ can_update_energy_cost: false, can_send_notifications: false });
  }, 20000);
});
