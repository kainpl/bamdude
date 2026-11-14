/**
 * The standalone wall at /camwall follows the tile size last picked in this
 * browser and takes `?size=s|m|l|xl` — the only way to size a wall on a TV,
 * whose storage nobody can reach. A kiosk never writes its size back.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api } from '../../api/client';
import { CamWallPage } from '../../pages/CamWallPage';

const wall = vi.hoisted(() => ({
  props: [] as { tileSize?: number; onChangeTileSize?: (size: number) => void; hideSettings?: boolean }[],
}));

vi.mock('../../components/CameraWall', () => ({
  CameraWall: (props: { tileSize?: number; onChangeTileSize?: (size: number) => void; hideSettings?: boolean }) => {
    wall.props.push(props);
    return (
      <div>
        <span>tile size {props.tileSize}</span>
        {props.onChangeTileSize && (
          <button type="button" onClick={() => props.onChangeTileSize?.(4)}>
            pick XL
          </button>
        )}
      </div>
    );
  },
}));

const KEY = 'camWallTileSize';

function renderWall(query = '') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/camwall${query}`]}>
        <CamWallPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('CamWallPage tile size', () => {
  beforeEach(() => {
    wall.props.length = 0;
    localStorage.removeItem(KEY);
    vi.spyOn(api, 'getPrinters').mockResolvedValue([]);
    vi.spyOn(api, 'getCameras').mockResolvedValue([]);
    vi.spyOn(api, 'getCamWallPrinters').mockResolvedValue([]);
    vi.spyOn(api, 'getCamWallCameras').mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.removeItem(KEY);
  });

  it('is M when nothing says otherwise', async () => {
    renderWall();

    await waitFor(() => expect(screen.getByText('tile size 2')).toBeInTheDocument());
  });

  it('follows the size last picked in this browser', async () => {
    localStorage.setItem(KEY, '3');
    renderWall();

    await waitFor(() => expect(screen.getByText('tile size 3')).toBeInTheDocument());
  });

  it.each([
    ['s', 1],
    ['m', 2],
    ['l', 3],
    ['xl', 4],
    ['XL', 4],
  ])('takes ?size=%s over what the browser remembers', async (param, size) => {
    localStorage.setItem(KEY, size === 3 ? '1' : '3');
    renderWall(`?size=${param}`);

    await waitFor(() => expect(screen.getByText(`tile size ${size}`)).toBeInTheDocument());
  });

  it.each(['huge', '', '7', 'xxl', '__proto__'])('falls back from ?size=%s to the remembered size', async (param) => {
    localStorage.setItem(KEY, '4');
    renderWall(`?size=${encodeURIComponent(param)}`);

    await waitFor(() => expect(screen.getByText('tile size 4')).toBeInTheDocument());
  });

  it('falls back to M from a remembered value that is not a size', async () => {
    localStorage.setItem(KEY, 'banana');
    renderWall('?size=nope');

    await waitFor(() => expect(screen.getByText('tile size 2')).toBeInTheDocument());
  });

  it('remembers a size picked on the signed-in wall', async () => {
    renderWall();

    await waitFor(() => expect(screen.getByText('pick XL')).toBeInTheDocument());
    screen.getByText('pick XL').click();

    await waitFor(() => expect(screen.getByText('tile size 4')).toBeInTheDocument());
    expect(localStorage.getItem(KEY)).toBe('4');
  });

  it('does not let a size in the URL overwrite the remembered one', async () => {
    localStorage.setItem(KEY, '1');
    renderWall('?size=xl');

    await waitFor(() => expect(screen.getByText('tile size 4')).toBeInTheDocument());
    expect(localStorage.getItem(KEY)).toBe('1');
  });

  it('sizes a kiosk from its URL and writes nothing back', async () => {
    localStorage.setItem(KEY, '1');
    renderWall('?token=tv-token&size=l');

    await waitFor(() => expect(screen.getByText('tile size 3')).toBeInTheDocument());
    expect(localStorage.getItem(KEY)).toBe('1');
    // Nobody stands at a kiosk: it has no control to pick a size with.
    expect(screen.queryByText('pick XL')).not.toBeInTheDocument();
    expect(wall.props.at(-1)?.hideSettings).toBe(true);
  });

  it('keeps a kiosk without a size in its URL from borrowing nothing but the remembered size', async () => {
    localStorage.setItem(KEY, '4');
    renderWall('?token=tv-token');

    // Same rule as the interval and the status overlay: a kiosk started from a
    // browser that had tuned the wall inherits those settings, and never writes.
    await waitFor(() => expect(screen.getByText('tile size 4')).toBeInTheDocument());
    expect(localStorage.getItem(KEY)).toBe('4');
  });
});
