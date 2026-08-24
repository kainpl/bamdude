/**
 * The authored-families management block: push per cloud, and the Orca
 * conflict dialog with the two explicit answers (overwrite cloud / adopt).
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { AuthoredFamiliesSection } from '../../components/AuthoredFamiliesSection';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const family = {
  filament_id: 'Pabc1234',
  alias: 'Poly PETG B',
  vendor: 'Poly',
  filament_type: 'PETG',
  presets: [
    {
      row_id: 7,
      name: 'Poly PETG B @P1S',
      bambu_pushed_id: null,
      bambu_dirty: false,
      orca_profile_id: 'uuid-1',
      orca_dirty: true,
    },
  ],
};

let resolveCalls: Array<{ preset_row_id: number; action: string }>;

beforeEach(() => {
  resolveCalls = [];
  server.use(
    http.get('/api/v1/filament-families/authored', () => HttpResponse.json({ families: [family] })),
    http.get('/api/v1/filament-families/authoring-options', () =>
      HttpResponse.json({ filament_types: ['PETG'], printer_names: [], push: { bambu: true, orca: true } }),
    ),
    http.get('/api/v1/cloud/status', () => HttpResponse.json({ is_authenticated: false })),
    http.get('/api/v1/orca-cloud/status', () =>
      HttpResponse.json({ connected: true, email: null, user_id: 'u', scope: 'external_app:connect sync:write' }),
    ),
    http.post('/api/v1/filament-families/:id/push', () =>
      HttpResponse.json({
        results: [
          {
            name: 'Poly PETG B @P1S',
            status: 'conflict',
            profile_id: 'uuid-1',
            row_id: 7,
            server_updated_time: 999,
            detail: null,
          },
        ],
      }),
    ),
    http.post('/api/v1/filament-families/:id/push-resolve', async ({ request }) => {
      resolveCalls.push((await request.json()) as { preset_row_id: number; action: string });
      return HttpResponse.json({ status: 'overwritten', profile_id: 'uuid-1' });
    }),
  );
});

describe('AuthoredFamiliesSection', () => {
  it('a conflicted push opens the dialog and force resolves it', async () => {
    render(<AuthoredFamiliesSection />);
    await waitFor(() => expect(screen.getByText('Poly PETG B')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Orca/ }));
    await waitFor(() => expect(screen.getByTestId('push-conflict-row')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Overwrite cloud copy' }));
    await waitFor(() => expect(resolveCalls).toEqual([{ preset_row_id: 7, action: 'force' }]));
    await waitFor(() => expect(screen.queryByTestId('push-conflict-row')).not.toBeInTheDocument());
  });

  it('the bambu button stays disabled while Bambu Cloud is disconnected', async () => {
    render(<AuthoredFamiliesSection />);
    await waitFor(() => expect(screen.getByText('Poly PETG B')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /Bambu/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /Orca/ })).toBeEnabled();
  });
});
