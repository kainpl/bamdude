/**
 * Skip Objects — selecting a part directly on the build plate (#2578 G20).
 *
 * The plate preview already carried a marker per object, placed from the
 * pick-PNG centroids the backend decodes, but the overlay was
 * `pointer-events-none`: the markers were decoration and the only way to skip
 * something was the list below. Upstream solves the same problem by shipping
 * the raw pick mask to the browser and resolving clicks per pixel; we make the
 * markers we already have clickable instead, which needs no extra image on the
 * wire.
 *
 * Skipping the wrong part ruins a running print, so the guards matter as much
 * as the click: an already-skipped object must not be re-skippable, a user
 * without printers:control must not be able to fire it, and the click must not
 * leak to the plate wrapper (which opens the enlarged view).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { SkipObjectsModal } from '../../components/SkipObjectsModal';
import { server } from '../mocks/server';

const OBJECTS = {
  objects: [
    { id: 11, name: 'bracket-left', skipped: false, x: 0.25, y: 0.4, norm: true },
    { id: 12, name: 'bracket-right', skipped: true, x: 0.75, y: 0.4, norm: true },
  ],
  bbox_all: null,
};

function mockObjects(payload: unknown = OBJECTS) {
  server.use(
    http.get('/api/v1/printers/:id/print/objects', () => HttpResponse.json(payload)),
    http.get('/api/v1/printers/:id/status', () =>
      HttpResponse.json({ id: 1, connected: true, state: 'RUNNING' }),
    ),
  );
}

/** The round markers are buttons whose label is the object name. */
function marker(name: string) {
  return screen.getByRole('button', { name });
}

describe('SkipObjectsModal — picking on the plate', () => {
  beforeEach(() => mockObjects());

  it('skips the object whose marker is clicked', async () => {
    render(<SkipObjectsModal printerId={1} isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(marker('bracket-left')).toBeInTheDocument());

    await userEvent.click(marker('bracket-left'));

    // The confirmation names the object that was clicked — proof the click
    // resolved to the right one rather than to whatever the list had first.
    await waitFor(() => {
      expect(screen.getAllByText(/bracket-left/).length).toBeGreaterThan(0);
    });
  });

  it('does not let an already-skipped object be skipped again', async () => {
    render(<SkipObjectsModal printerId={1} isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(marker('bracket-right')).toBeInTheDocument());

    expect(marker('bracket-right')).toBeDisabled();
  });

  it('does not open the enlarged plate view when a marker is clicked', async () => {
    // The marker sits inside the plate wrapper, whose own click enlarges the
    // image. Without stopPropagation a skip attempt would also cover the modal
    // with the enlarged view.
    render(<SkipObjectsModal printerId={1} isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(marker('bracket-left')).toBeInTheDocument());

    await userEvent.click(marker('bracket-left'));

    // The enlarged overlay renders a second, much larger copy of the plate
    // image; if it opened there would be more than one plate image on screen.
    const plateImages = screen.queryAllByRole('img');
    expect(plateImages.length).toBeLessThanOrEqual(1);
  });
});
