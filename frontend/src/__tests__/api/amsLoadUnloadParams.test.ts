/**
 * The load / unload routes take optional parameters, and "optional" has to mean
 * absent, never the string "undefined" and never a dropped 0: the backend
 * validates extruder_id as 0-1 and tray_id as an addressable slot (upstream
 * 9500c046).
 */

import { describe, it, expect, afterEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { api } from '../../api/client';

let lastSearch: string | null = null;

function capture() {
  server.use(
    http.post('/api/v1/printers/:id/ams/load', ({ request }) => {
      lastSearch = new URL(request.url).search;
      return HttpResponse.json({ success: true, tray_id: 5 });
    }),
    http.post('/api/v1/printers/:id/ams/unload', ({ request }) => {
      lastSearch = new URL(request.url).search;
      return HttpResponse.json({ success: true });
    })
  );
}

afterEach(() => {
  lastSearch = null;
});

describe('AMS load/unload query parameters', () => {
  it('omits extruder_id when no hotend was chosen', async () => {
    capture();
    await api.amsLoadFilament(1, 5);
    expect(lastSearch).toBe('?tray_id=5');
  });

  it('sends extruder_id 0 rather than dropping it as falsy', async () => {
    capture();
    await api.amsLoadFilament(1, 5, 0);
    expect(lastSearch).toBe('?tray_id=5&extruder_id=0');
  });

  it('omits tray_id on an unaddressed unload', async () => {
    capture();
    await api.amsUnloadFilament(1);
    expect(lastSearch).toBe('');
  });

  it('sends tray_id 0 on an unload of the first slot', async () => {
    capture();
    await api.amsUnloadFilament(1, 0);
    expect(lastSearch).toBe('?tray_id=0');
  });
});
