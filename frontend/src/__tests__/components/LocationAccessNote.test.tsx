/**
 * N62 §5.6: moving a printer out of (or into) a location a limited group is given changes what
 * that group reaches. An administrator is told which groups before saving; nobody else sees it,
 * since the server refuses them the move.
 */

import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { LocationAccessNote } from '../../components/LocationAccessNote';

function serve() {
  server.use(
    http.get('/api/v1/groups/', () =>
      HttpResponse.json([
        { id: 2, name: 'Team A', restrict_printers: true, location_ids: [10], printer_ids: [], permissions: [], is_system: false, user_count: 1 },
        { id: 3, name: 'Open', restrict_printers: false, location_ids: [20], printer_ids: [], permissions: [], is_system: false, user_count: 1 },
      ]),
    ),
    http.get('/api/v1/printer-locations', () =>
      HttpResponse.json({
        locations: [
          { id: 10, name: 'Hall', parent_id: null, path: 'Hall', depth: 1, printer_count: 0, sensor_count: 0, queued_count: 0 },
          { id: 11, name: 'Rack', parent_id: 10, path: 'Hall / Rack', depth: 2, printer_count: 1, sensor_count: 0, queued_count: 0 },
          { id: 20, name: 'Lab', parent_id: null, path: 'Lab', depth: 1, printer_count: 1, sensor_count: 0, queued_count: 0 },
        ],
      }),
    ),
  );
}

describe('LocationAccessNote', () => {
  it('names the limited group whose location (or one above it) the move leaves', async () => {
    serve();
    render(<LocationAccessNote from={11} to={20} />);
    expect(await screen.findByRole('status')).toHaveTextContent('Team A');
    // A group without a limit reaches every printer: moving changes nothing for it.
    expect(screen.getByRole('status')).not.toHaveTextContent('Open');
  });

  it('says nothing when the printer stays where it is', async () => {
    serve();
    render(<LocationAccessNote from={11} to={11} />);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('says nothing to someone who is not an administrator', async () => {
    serve();
    server.use(
      http.get('/api/v1/auth/me', () =>
        HttpResponse.json({
          id: 9, username: 'op', role: 'user', is_active: true, is_admin: false,
          groups: [], permissions: ['printers:update'], created_at: '2024-01-01T00:00:00Z',
        }),
      ),
    );
    render(<LocationAccessNote from={11} to={20} />);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});
