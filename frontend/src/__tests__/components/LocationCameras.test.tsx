/**
 * The cameras a location's group header offers.
 *
 * A group header must never break, so the interesting cases here are the ones
 * that render nothing: no location, no camera filed there, a camera switched
 * off, no permission. The button itself only has to open the right thing.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { LocationCameras } from '../../components/LocationCameras';

const camera = (overrides: Record<string, unknown> = {}) => ({
  id: 1,
  name: 'Shelf',
  camera_type: 'mjpeg',
  url: 'http://192.0.2.50/stream',
  snapshot_url: null,
  rotation: 0,
  enabled: true,
  location_id: 5,
  location_name: 'Room A',
  ...overrides,
});

const serve = (rows: ReturnType<typeof camera>[]) =>
  server.use(http.get('/api/v1/cameras/', () => HttpResponse.json(rows)));

describe('LocationCameras', () => {
  beforeEach(() => {
    serve([camera()]);
  });

  it('offers a button for each camera filed under this location', async () => {
    serve([camera(), camera({ id: 2, name: 'Dryer' })]);
    render(<LocationCameras locationId={5} />);

    expect(await screen.findByText('Shelf')).toBeInTheDocument();
    expect(screen.getByText('Dryer')).toBeInTheDocument();
  });

  it('shows nothing for the group without a location', async () => {
    render(<LocationCameras locationId={null} />);
    await waitFor(() => expect(screen.queryByText('Shelf')).not.toBeInTheDocument());
  });

  it('shows nothing for a location no camera watches', async () => {
    serve([camera({ location_id: 99 })]);
    render(<LocationCameras locationId={5} />);
    await waitFor(() => expect(screen.queryByText('Shelf')).not.toBeInTheDocument());
  });

  it('leaves out a camera the operator switched off', async () => {
    serve([camera({ enabled: false })]);
    render(<LocationCameras locationId={5} />);
    await waitFor(() => expect(screen.queryByText('Shelf')).not.toBeInTheDocument());
  });

  it('opens the floating window when the farm uses one', async () => {
    const onOpenEmbedded = vi.fn();
    render(<LocationCameras locationId={5} onOpenEmbedded={onOpenEmbedded} />);

    await userEvent.setup().click(await screen.findByText('Shelf'));
    expect(onOpenEmbedded).toHaveBeenCalledWith(1, 'Shelf');
  });

  it('opens a browser window at the standalone route otherwise', async () => {
    const open = vi.fn();
    vi.stubGlobal('open', open);
    render(<LocationCameras locationId={5} />);

    await userEvent.setup().click(await screen.findByText('Shelf'));
    expect(open).toHaveBeenCalled();
    expect(open.mock.calls[0][0]).toBe('/camera/standalone/1');
    vi.unstubAllGlobals();
  });
});
