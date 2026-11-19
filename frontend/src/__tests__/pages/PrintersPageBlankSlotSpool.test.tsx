/**
 * A slot the printer reports blank still shows the spool BamDude holds for it
 * (upstream 1cc7f4f18, #3434; owner decision Р34).
 *
 * After a runout the printer blanks the slot. A reel put in and assigned stays
 * loaded-but-blank until the printer takes a setting for it, and the card used
 * to show only "?" with a Configure button: the operator could neither see the
 * spool held for the slot nor replace or unassign it — and a "?" slot with no
 * spool offered no way to say which reel went in.
 */
import { describe, it, expect } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockPrinter = {
  id: 1, name: 'Bench', ip_address: '192.168.1.100', serial_number: '0948BB540200427', access_code: '12345678',
  model: 'P1S', enabled: true, nozzle_count: 1, nozzle_diameter: 0.4, nozzle_type: 'hardened_steel',
  location: 'Workshop', created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
};

const loaded = {
  tray_color: 'FF0000FF', tray_type: 'PLA', tray_sub_brands: 'PLA Basic', tray_id_name: 'A00-R0',
  tray_info_idx: 'GFA00', remain: 80, k: 0.02, cali_idx: null, tag_uid: null, tray_uuid: null,
  nozzle_temp_min: 190, nozzle_temp_max: 230, drying_temp: 55, drying_time: 8, state: 11, exists: true,
};
// Presence bit on, nothing configured: the "?" slot.
const blank = { tray_color: '', tray_type: '', tray_sub_brands: '', remain: -1, state: 11, exists: true };
// Presence bit off: an empty slot.
const empty = { tray_color: '', tray_type: '', tray_sub_brands: '', remain: -1, state: 9, exists: false };

const STATUS = {
  connected: true, state: 'IDLE', progress: 0, layer_num: 0, total_layers: 0,
  temperatures: { nozzle: 25, bed: 25, chamber: 25 },
  remaining_time: 0, filename: null, wifi_signal: -29, speed_level: 2, vt_tray: [],
  ams: [{
    id: 0, humidity: 20, temp: 30, is_ams_ht: false, serial_number: 'AMS0', sw_ver: '01.00.00.00', module_type: 'ams',
    tray: [{ id: 0, ...loaded }, { id: 1, ...blank }, { id: 2, ...blank }, { id: 3, ...empty }],
  }],
  ams_extruder_map: {}, fila_switch: null, ams_switch_inlet: {}, extruder_slots: {},
};

const ASSIGNMENT = {
  id: 5, spool_id: 9, printer_id: 1, ams_id: 0, tray_id: 1,
  spool: { id: 9, material: 'PETG', brand: 'Acme', subtype: null, color_name: 'Grey', rgba: '858585FF', label_weight: 1000, weight_used: 0 },
};

function renderCard() {
  const deleted: string[] = [];
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinter])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(STATUS)),
    http.get('/api/v1/printers/status/batch', ({ request }) =>
      HttpResponse.json(Object.fromEntries(new URL(request.url).searchParams.getAll('ids').map((id) => [id, STATUS])))),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.get('/api/v1/spoolman/status', () => HttpResponse.json({ enabled: false, connected: false })),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([ASSIGNMENT])),
    http.delete('/api/v1/inventory/assignments/:printer/:ams/:tray', ({ params }) => {
      deleted.push(`${params.printer}/${params.ams}/${params.tray}`);
      return HttpResponse.json({ status: 'deleted' });
    }),
  );
  render(<PrintersPage />);
  return deleted;
}

/** Hover the n-th blank slot of the card (0-based among the slots that report no filament). */
async function hoverBlankSlot(index: number) {
  await screen.findByText('AMS-A', {}, { timeout: 5000 });
  const slots = await screen.findAllByTestId('empty-slot');
  fireEvent.mouseEnter(slots[index]);
  return within(await screen.findByTestId('empty-slot-hover-card'));
}

describe('PrintersPage — a blank slot and the spool held for it', () => {
  it('names the spool, says the printer has not taken it, and offers Replace and Unassign', async () => {
    const deleted = renderCard();
    // The slot is drawn in the spool's colour, marked as not taken yet.
    await waitFor(() => expect(document.querySelectorAll('[data-pending-config="true"]')).toHaveLength(1), { timeout: 5000 });

    const card = await hoverBlankSlot(0);
    expect(card.getByText(/Acme PETG/)).toBeInTheDocument();
    expect(card.getByText(/the printer has not taken it yet/i)).toBeInTheDocument();
    expect(card.getByRole('button', { name: /Replace spool/i })).toBeInTheDocument();
    expect(card.queryByRole('button', { name: /^Assign Spool$/i })).toBeNull();

    fireEvent.click(card.getByRole('button', { name: /Unassign/i }));
    await waitFor(() => expect(deleted).toEqual(['1/0/1']));
  });

  it('Replace opens the usual assignment dialog over the spool it holds', async () => {
    renderCard();
    const card = await hoverBlankSlot(0);
    fireEvent.click(card.getByRole('button', { name: /Replace spool/i }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/Acme PETG/)).toBeInTheDocument();
  });

  it('a "?" slot with no spool offers Assign Spool', async () => {
    renderCard();
    const card = await hoverBlankSlot(1);
    expect(card.getByRole('button', { name: /^Assign Spool$/i })).toBeInTheDocument();
    expect(card.queryByRole('button', { name: /Unassign/i })).toBeNull();
  });

  it('an empty slot offers no assignment', async () => {
    renderCard();
    const card = await hoverBlankSlot(2);
    expect(card.getByText('Empty slot')).toBeInTheDocument();
    expect(card.queryByRole('button', { name: /Assign Spool/i })).toBeNull();
  });
});
