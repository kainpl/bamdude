/**
 * The Edit Printer dialog is where the backup-compatibility policy is set, and
 * PATCH /printers/{id} replaces the whole namespace: what leaves this form IS
 * the policy. Mounted directly — driving the page to open the dialog would test
 * the card menu, not this.
 */

import { describe, it, expect } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { EditPrinterModal } from '../../pages/PrintersPage';
import type { Printer } from '../../api/client';

const printer = {
  id: 1,
  name: 'X1 Carbon',
  serial_number: '00M09A350100001',
  ip_address: '192.168.1.100',
  model: 'X1C',
  location: null,
  location_id: null,
  tags: [],
  tag_ids: [],
  nozzle_count: 1,
  is_active: true,
  archived: false,
  archived_at: null,
  auto_archive: true,
  cleanup_after_print: false,
  mqtt_connection_timeout: 0,
  external_camera_url: null,
  external_camera_type: null,
  external_camera_enabled: false,
  external_camera_snapshot_url: null,
  camera_rotation: 0,
  plate_detection_enabled: false,
  stagger_interval_minutes: 0,
  swap_mode_enabled: false,
  swap_profile: null,
  require_plate_clear: true,
  ams_policies: {
    backup_compatibility: { normalize_color: false, canonical_color_rgba: '000000FF', generic_base_material: false },
  },
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
} as unknown as Printer;

describe('Edit Printer — AMS backup compatibility', () => {
  it('sends the whole namespace with an opaque RRGGBBFF colour', async () => {
    type PatchBody = { ams_policies: { backup_compatibility: Record<string, unknown> } };
    const patches: PatchBody[] = [];
    server.use(
      http.get('/api/v1/macros/swap-profiles', () => HttpResponse.json([])),
      http.post('/api/v1/printers/diagnostic', () => HttpResponse.json({ checks: [] })),
      http.patch('/api/v1/printers/1', async ({ request }) => {
        const body = (await request.json()) as PatchBody;
        patches.push(body);
        return HttpResponse.json({ ...printer, ...body });
      })
    );

    render(<EditPrinterModal printer={printer} onClose={() => {}} />);
    await userEvent.click(await screen.findByLabelText('Advertise one canonical colour to the AMS'));
    // The picker hands over `#rrggbb`; the form stores that raw and
    // `backupCompatibilityPatch` is the only thing that normalises it.
    fireEvent.change(screen.getByLabelText('Opaque colour every eligible slot is reported as'), {
      target: { value: '#1a2b3c' },
    });
    await userEvent.click(screen.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() => expect(patches).toHaveLength(1));
    const policy = patches[0].ams_policies.backup_compatibility;
    expect(policy.normalize_color).toBe(true);
    expect(policy.generic_base_material).toBe(false);
    expect(policy.canonical_color_rgba).toMatch(/^[0-9A-F]{6}FF$/);
    expect(policy.canonical_color_rgba).toBe('1A2B3CFF');
  });
});
