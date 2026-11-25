/**
 * Tests for the mass firmware update page.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
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
    { printer_id: 1, current_version: '01.00.00.00', latest_version: '01.02.00.00', update_available: true },
    { printer_id: 2, current_version: '01.00.00.00', latest_version: '01.02.00.00', update_available: true },
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

  it('does not start a wiki-only update without an offline firmware file', async () => {
    server.use(
      http.get('/api/v1/firmware/updates', () => HttpResponse.json({
        updates: [{ printer_id: 1, current_version: '01.02.00.00', latest_version: '01.03.00.00', update_available: true }],
        updates_available: 1,
      })),
      http.post('/api/v1/firmware/batch/preview', () => HttpResponse.json({
        groups: [{
          model: 'P1S', printer_ids: [1], available_versions: ['01.02.00.00'],
          cached_versions: [], default_version: '01.02.00.00', remote_apply: false,
          skipped_printer_ids: [],
        }],
      })),
    );

    render(<FirmwareUpdatePage />);
    expect(await screen.findByText(/01\.03\.00\.00 has been announced/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Upgrade \(0\)/i })).toBeDisabled();
    expect(screen.queryByRole('option', { name: '01.03.00.00' })).not.toBeInTheDocument();
  });

  it('offers an older installable update when the newest announced version has no file', async () => {
    server.use(
      http.get('/api/v1/firmware/updates', () => HttpResponse.json({
        updates: [{ printer_id: 1, current_version: '01.01.00.00', latest_version: '01.03.00.00', update_available: true }],
        updates_available: 1,
      })),
      http.post('/api/v1/firmware/batch/preview', () => HttpResponse.json({
        groups: [{
          model: 'P1S', printer_ids: [1], available_versions: ['01.02.00.00'],
          cached_versions: [], default_version: '01.02.00.00', remote_apply: false,
          skipped_printer_ids: [],
        }],
      })),
    );

    render(<FirmwareUpdatePage />);
    expect(await screen.findByRole('button', { name: /Upgrade \(1\)/i })).toBeEnabled();
    expect(screen.getByText(/01\.03\.00\.00 has been announced/)).toBeInTheDocument();
  });

  it('shows past runs (incl. single-source) in the update log tab', async () => {
    server.use(
      http.get('/api/v1/firmware/batch', () =>
        HttpResponse.json([
          {
            id: 5,
            created_at: '2026-05-25T10:00:00',
            source: 'single',
            status: 'completed',
            total: 1,
            succeeded: 1,
            skipped: 0,
            failed: 0,
            items: [
              {
                printer_id: 1,
                model: 'P1S',
                from_version: '01.00.00.00',
                to_version: '01.02.00.00',
                status: 'uploaded',
                message: null,
                error: null,
              },
            ],
          },
        ]),
      ),
    );

    render(<FirmwareUpdatePage />);
    await userEvent.click(screen.getByRole('button', { name: /Update log/i }));

    await waitFor(() => expect(screen.getByText('Single')).toBeInTheDocument());
    expect(screen.getByText(/01\.00\.00\.00/)).toBeInTheDocument();
  });

  it('restores an active batch and replaces its progress and terminal states using polling alone', async () => {
    const common = {
      model: 'P1S', from_version: '01.00.00.00', to_version: '01.02.00.00', message: null, error: null,
    };
    const running = {
      id: 12, created_at: null, source: 'bulk', status: 'running', total: 2, succeeded: 0, skipped: 0, failed: 0,
      items: [
        { ...common, printer_id: 1, status: 'uploading', percent: 42 },
        { ...common, printer_id: 2, status: 'pending', percent: null },
      ],
    };
    let complete = false;
    server.use(
      http.get('/api/v1/firmware/batch', ({ request }) =>
        HttpResponse.json(complete && new URL(request.url).searchParams.has('active_only') ? [] : [running])),
      http.get('/api/v1/firmware/batch/12', () => HttpResponse.json(complete ? {
        ...running, status: 'completed', succeeded: 1, failed: 1,
        items: [
          { ...common, printer_id: 1, status: 'uploaded', percent: 100, message: 'firmware.manualApply.a1' },
          { ...common, printer_id: 2, status: 'failed', percent: null, error: 'Printer refused storage (553)' },
        ],
      } : running)),
    );
    render(<FirmwareUpdatePage />);
    const batch = await screen.findByRole('heading', { name: 'Upgrade Status · #12' });
    const panel = within(batch.parentElement!);
    expect(await panel.findByRole('progressbar', { name: 'Alpha: Upgrade Status' })).toHaveAttribute('value', '42');
    expect(panel.getByText('Pending')).toBeInTheDocument();
    const upgrade = await screen.findByRole('button', { name: /Upgrade \(1\)/i }, { timeout: 5000 });
    expect(upgrade).toBeDisabled();
    complete = true;
    expect(await panel.findByText('Transferred to SD card', undefined, { timeout: 5000 })).toBeInTheDocument();
    expect(panel.getByText('Printer refused storage (553)')).toBeInTheDocument();
    expect(panel.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(upgrade).toBeEnabled();
  }, 10_000);
});
