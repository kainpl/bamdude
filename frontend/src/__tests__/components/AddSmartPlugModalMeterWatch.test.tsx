/**
 * The plug form's meter-watch settings (spec plug-meter-watchdog W3-W5): what the operator picks
 * is what the server is sent, an empty field is the server's default, and a threshold below the
 * agreed floor never leaves the form.
 */
import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { AddSmartPlugModal } from '../../components/AddSmartPlugModal';
import type { SmartPlug } from '../../api/client';

const plug = {
  id: 7,
  name: 'Bench',
  plug_type: 'tasmota',
  ip_address: '10.0.0.5',
  enabled: true,
  printer_id: null,
  meter_min_expected_wh: null,
  mqtt_expected_power_interval_seconds: null,
  phantom_watts_risk: null,
} as unknown as SmartPlug;

describe('AddSmartPlugModal — meter watch', () => {
  it('refuses a missed-energy threshold below 100 Wh', async () => {
    const user = userEvent.setup();
    const sent = vi.fn();
    server.use(
      http.patch('/api/v1/smart-plugs/7', async ({ request }) => {
        sent(await request.json());
        return HttpResponse.json(plug);
      }),
    );
    render(<AddSmartPlugModal plug={plug} onClose={() => {}} />);

    const field = screen.getByLabelText('Missed energy before a stall is suspected (Wh)') as HTMLInputElement;
    await user.type(field, '50');
    await user.click(screen.getByRole('button', { name: /^Save$/ }));

    // The browser stops it at the field's own minimum; the form's check stands behind it.
    expect(field.validity.rangeUnderflow).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sent).not.toHaveBeenCalled();
  });

  it('sends the threshold and the phantom-watts policy it was given', async () => {
    const user = userEvent.setup();
    const sent = vi.fn();
    server.use(
      http.patch('/api/v1/smart-plugs/7', async ({ request }) => {
        sent(await request.json());
        return HttpResponse.json(plug);
      }),
    );
    render(<AddSmartPlugModal plug={plug} onClose={() => {}} />);

    await user.type(screen.getByLabelText('Missed energy before a stall is suspected (Wh)'), '150');
    await user.selectOptions(screen.getByLabelText('Watts after the relay opens'), 'risk');
    await user.click(screen.getByRole('button', { name: /^Save$/ }));

    await waitFor(() => expect(sent).toHaveBeenCalled());
    expect(sent.mock.calls[0][0]).toMatchObject({
      meter_min_expected_wh: 150,
      phantom_watts_risk: true,
      mqtt_expected_power_interval_seconds: null,
    });
  });

  it('leaves an untouched plug on the server defaults', async () => {
    const user = userEvent.setup();
    const sent = vi.fn();
    server.use(
      http.patch('/api/v1/smart-plugs/7', async ({ request }) => {
        sent(await request.json());
        return HttpResponse.json(plug);
      }),
    );
    render(<AddSmartPlugModal plug={plug} onClose={() => {}} />);

    await user.click(screen.getByRole('button', { name: /^Save$/ }));

    await waitFor(() => expect(sent).toHaveBeenCalled());
    expect(sent.mock.calls[0][0]).toMatchObject({ meter_min_expected_wh: null, phantom_watts_risk: null });
  });
});
