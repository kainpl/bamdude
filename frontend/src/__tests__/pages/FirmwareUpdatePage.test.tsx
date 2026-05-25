/**
 * Tests for the mass firmware update page.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { FirmwareUpdatePage } from '../../pages/FirmwareUpdatePage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockPrinters = [
  { id: 1, name: 'Alpha', model: 'P1S' },
  { id: 2, name: 'Bravo', model: 'P1S' },
];

const mockUpdates = {
  updates: [
    { printer_id: 1, current_version: '01.00.00.00', update_available: true },
    { printer_id: 2, current_version: '01.00.00.00', update_available: true },
  ],
  updates_available: 2,
};

// Bravo (id 2) is printing → backend marks it skipped.
const mockPreview = {
  groups: [
    {
      model: 'P1S',
      printer_ids: [1, 2],
      available_versions: ['01.02.00.00', '01.01.00.00'],
      cached_versions: ['01.01.00.00'],
      default_version: '01.02.00.00',
      remote_apply: false,
      skipped_printer_ids: [2],
    },
  ],
};

describe('FirmwareUpdatePage', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(mockPrinters)),
      http.get('/api/v1/firmware/updates', () => HttpResponse.json(mockUpdates)),
      http.post('/api/v1/firmware/batch/preview', () => HttpResponse.json(mockPreview)),
    );
  });

  it('shows a per-model tab with the printer count', async () => {
    render(<FirmwareUpdatePage />);
    await waitFor(() => expect(screen.getByText('P1S (2)')).toBeInTheDocument());
  });

  it('marks the printing printer as skipped and posts only eligible printers on Upgrade', async () => {
    let postedBody: unknown = null;
    server.use(
      http.post('/api/v1/firmware/batch', async ({ request }) => {
        postedBody = await request.json();
        return HttpResponse.json({ run_id: 7 });
      }),
    );

    render(<FirmwareUpdatePage />);

    await waitFor(() => expect(screen.getByText('Bravo')).toBeInTheDocument());
    // The printing printer is flagged.
    expect(screen.getByText(/skipped/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Upgrade/i }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    const body = postedBody as { targets: { printer_id: number; version?: string }[] };
    // Only Alpha (id 1) — Bravo (id 2) is skipped (printing).
    expect(body.targets).toHaveLength(1);
    expect(body.targets[0].printer_id).toBe(1);
    expect(body.targets[0].version).toBe('01.02.00.00');
  });
});
