/**
 * The slice modal asks for every project slot; the print path does not (#2712).
 *
 * One endpoint answers two questions. The filament list the slice modal builds
 * is positional all the way down to the CLI's `filament_N.json` parts, so a
 * source declaring four filaments but painting with slot 4 alone must still
 * present four rows — otherwise the user's single pick binds to slot 1 and
 * slot 4 slices with whatever the source had baked in. Print-time AMS matching
 * wants the opposite: exactly the spools the job consumes, or the operator is
 * asked to load three it will never touch.
 *
 * Both halves are pinned here, because the bug comes back just as surely by
 * widening the print path as by narrowing the modal's.
 */
import { describe, expect, it, vi } from 'vitest';
import { waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { SliceModal } from '../../components/SliceModal';
import { SliceJobTrackerProvider } from '../../contexts/SliceJobTrackerContext';
import { api } from '../../api/client';

function captureRequirementRequests(): string[] {
  const seen: string[] = [];
  server.use(
    // Single plate, so the modal does not stop at the plate picker — the
    // requirements query is disabled until a plate is settled.
    http.get('*/api/v1/library/files/:id/plates', () =>
      HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }] }),
    ),
    http.get('*/api/v1/library/files/:id/filament-requirements', ({ request }) => {
      seen.push(request.url);
      return HttpResponse.json({ file_id: 1, filename: 'Painted.3mf', filaments: [] });
    }),
    http.get('*/api/v1/archives/:id/filament-requirements', ({ request }) => {
      seen.push(request.url);
      return HttpResponse.json({ archive_id: 1, filename: 'Painted.3mf', plate_id: 1, filaments: [] });
    }),
  );
  return seen;
}

describe('filament requirements — full_slots', () => {
  it('the slice modal asks for every project slot', async () => {
    const seen = captureRequirementRequests();

    render(
      <SliceJobTrackerProvider>
        <SliceModal source={{ kind: 'libraryFile', id: 1, filename: 'Painted.3mf' }} onClose={() => undefined} />
      </SliceJobTrackerProvider>,
    );

    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    expect(seen.every((url) => url.includes('full_slots=true'))).toBe(true);
  });

  it('the api client omits the flag unless asked', () => {
    // Guards the default: every existing caller keeps the narrow list without
    // being edited, which is what makes this change safe to ship.
    // A fresh Response per call — one instance cannot be read three times.
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(async () => new Response('{}', { headers: { 'content-type': 'application/json' } }));

    void api.getLibraryFileFilamentRequirements(7, 1, undefined);
    void api.getArchiveFilamentRequirements(7, 1, undefined);
    void api.getLibraryFileFilamentRequirements(7, 1, undefined, true);

    const urls = spy.mock.calls.map((c) => String(c[0]));
    expect(urls[0]).not.toContain('full_slots');
    expect(urls[1]).not.toContain('full_slots');
    expect(urls[2]).toContain('full_slots=true');

    spy.mockRestore();
  });
});
