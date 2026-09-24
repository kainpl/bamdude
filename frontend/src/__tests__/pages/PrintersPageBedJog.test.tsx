/**
 * The printer card's Z jog (upstream #1334, audit decision D11, 2026-09-24).
 *
 * ⚠️ **The card's arrows speak BambuStudio's arrow convention** — "up" sends a
 * negative value, meaning "the part that travels on Z goes up" — and they go
 * through `/jog?axis=z`, where the backend applies the i3 bed-slinger flip. The
 * same button therefore sends the same number on every model; the frontend never
 * flips the sign (`inv-jog-mirrors-bambustudio`).
 *
 * `/bed-jog` is no longer the card's route: it now means a model-independent
 * nozzle-bed GAP for API clients, which is the opposite of the arrow on an i3
 * machine.
 *
 * ⚠️ **On an i3 bed-slinger the words change, not the numbers.** Its Z axis
 * carries the toolhead, so "Bed" / "Move plate up" describes a part that does
 * not move in Z. BambuStudio labels that column "Z"; so does the card.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

function printer(model: string) {
  return {
    id: 7,
    name: `Jog ${model}`,
    ip_address: '192.168.1.107',
    serial_number: '00M09A350100007',
    access_code: '12345678',
    model,
    enabled: true,
    nozzle_diameter: 0.4,
    nozzle_type: 'hardened_steel',
    location: null,
    tags: [],
    auto_archive: true,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  };
}

function status(isBedSlinger: boolean) {
  return {
    connected: true,
    state: 'IDLE',
    progress: 0,
    layer_num: 0,
    total_layers: 0,
    temperatures: { nozzle: 25, bed: 25, chamber: 25 },
    remaining_time: 0,
    filename: null,
    wifi_signal: -50,
    vt_tray: [],
    is_bed_slinger: isBedSlinger,
  };
}

let posted: string[] = [];

function serve(model: string, isBedSlinger: boolean) {
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer(model)])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status(isBedSlinger))),
    http.get('/api/v1/printers/status/batch', () => HttpResponse.json({ 7: status(isBedSlinger) })),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.get('/api/v1/printer-tags', () => HttpResponse.json({ tags: [] })),
    http.post('/api/v1/printers/:id/jog', ({ request }) => {
      posted.push(new URL(request.url).pathname + new URL(request.url).search);
      return HttpResponse.json({ success: true, axis: 'z', distance: -10 });
    }),
    http.post('/api/v1/printers/:id/bed-jog', ({ request }) => {
      posted.push(new URL(request.url).pathname + new URL(request.url).search);
      return HttpResponse.json({ success: true, message: 'ok' });
    }),
  );
}

async function openJogMenu(title: string) {
  const card = await waitFor(() => {
    const el = document.getElementById('printer-7');
    expect(el).not.toBeNull();
    return el!;
  });
  await userEvent.click(await within(card).findByTitle(title));
}

beforeEach(() => {
  posted = [];
  // Past the not-homed prompt: this is what a successful Auto Home sets.
  sessionStorage.setItem('bamdude.bedJog.warned.7', '1');
});

afterEach(() => {
  sessionStorage.removeItem('bamdude.bedJog.warned.7');
});

describe('printer card Z jog', () => {
  it('sends BambuStudio\'s arrow value through /jog, not /bed-jog', async () => {
    serve('X1C', false);
    render(<PrintersPage />);

    await openJogMenu('Move build plate');
    await userEvent.click(screen.getByLabelText('Move plate up'));

    await waitFor(() => expect(posted).toEqual(['/api/v1/printers/7/jog?axis=z&distance=-10&extruder_index=0']));
  });

  it('sends the same number on a bed-slinger — the flip is the backend\'s', async () => {
    serve('A1', true);
    render(<PrintersPage />);

    await openJogMenu('Move toolhead (Z)');
    await userEvent.click(screen.getByLabelText('Move toolhead up'));

    await waitFor(() => expect(posted).toEqual(['/api/v1/printers/7/jog?axis=z&distance=-10&extruder_index=0']));
  });

  it('names the toolhead on a bed-slinger, the plate elsewhere', async () => {
    serve('A1', true);
    render(<PrintersPage />);

    await openJogMenu('Move toolhead (Z)');

    expect(screen.getByLabelText('Move toolhead up')).toBeInTheDocument();
    expect(screen.getByLabelText('Move toolhead down')).toBeInTheDocument();
    expect(screen.queryByLabelText('Move plate up')).not.toBeInTheDocument();
  });
});
