/**
 * The Cam Wall's tile size (S · M · L · XL) sets the grid — and only the grid.
 * However many tiles fit in a row, the wall still runs ONE live stream, the
 * one the operator picked; every other visible tile is a snapshot.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { CameraWall, type CameraWallStatus } from '../../components/CameraWall';
import { camWallGridClass, camWallTileSizeFromParam } from '../../utils/camWallTileSize';

const printers = [
  { id: 101, name: 'X1C-A' },
  { id: 102, name: 'P1S-B' },
  { id: 103, name: 'A1-C' },
];

function connectedStatuses() {
  return new Map<number, CameraWallStatus>(printers.map((p) => [p.id, { connected: true, state: 'IDLE' }]));
}

class VisibleIntersectionObserver {
  private callback: IntersectionObserverCallback;
  root = null;
  rootMargin = '';
  thresholds = [];

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
  }

  observe = (target: Element) => {
    this.callback([{ target, isIntersecting: true } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
  };
  unobserve = vi.fn();
  disconnect = vi.fn();
  takeRecords = () => [];
}

const M_GRID = 'grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4';

describe('camWallGridClass', () => {
  it('is the grid the wall always had at M, and for anything that is not a size', () => {
    expect(camWallGridClass(2)).toBe(M_GRID);
    for (const junk of [undefined, null, 0, 5, -1, 2.5, Number.NaN]) {
      expect(camWallGridClass(junk)).toBe(M_GRID);
    }
  });

  it('fits up to five tiles in a row at S, two at L and one at XL', () => {
    expect(camWallGridClass(1)).toContain('xl:grid-cols-5');
    expect(camWallGridClass(3)).toContain('lg:grid-cols-2');
    expect(camWallGridClass(3)).not.toMatch(/grid-cols-[345]/);
    expect(camWallGridClass(4)).toContain('grid-cols-1');
    expect(camWallGridClass(4)).not.toMatch(/grid-cols-[2345]/);
  });
});

describe('camWallTileSizeFromParam', () => {
  it('reads s, m, l and xl in any case', () => {
    expect(['s', 'm', 'l', 'xl', 'S', 'XL', 'Xl'].map(camWallTileSizeFromParam)).toEqual([1, 2, 3, 4, 1, 4, 4]);
  });

  it('is null for anything else', () => {
    for (const junk of [null, undefined, '', 'xxl', 'large', '3', ' xl', 'constructor', '__proto__', 'toString']) {
      expect(camWallTileSizeFromParam(junk)).toBeNull();
    }
  });
});

describe('CameraWall tile size', () => {
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', VisibleIntersectionObserver);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('jpeg', { status: 200 })));
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = vi.fn(() => 'blob:snapshot');
      static revokeObjectURL = vi.fn();
    });
    Object.defineProperty(HTMLImageElement.prototype, 'decode', {
      configurable: true,
      value: vi.fn().mockResolvedValue(undefined),
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  function renderWall(tileSize?: number, onChangeTileSize?: (size: number) => void) {
    return render(
      <CameraWall
        printers={printers}
        snapshotIntervalSec={10}
        statusMode="compact"
        statusOverride={connectedStatuses()}
        // What makes a wall interactive: without it no tile can go live at all.
        onOpenPrinterCard={vi.fn()}
        onChangeSnapshotIntervalSec={vi.fn()}
        onChangeStatusMode={vi.fn()}
        tileSize={tileSize}
        onChangeTileSize={onChangeTileSize}
      />,
    );
  }

  const grid = () => screen.getByRole('button', { name: 'Watch X1C-A live' }).closest('.grid') as HTMLElement;

  it('keeps the grid it always had when no size is given', async () => {
    renderWall();

    await waitFor(() => expect(grid()).not.toBeNull());
    expect(grid().className).toBe(M_GRID);
  });

  it.each([1, 2, 3, 4])('lays the tiles out for size %i', async (size) => {
    renderWall(size);

    await waitFor(() => expect(grid()).not.toBeNull());
    expect(grid().className).toBe(camWallGridClass(size));
  });

  it.each([1, 2, 3, 4])('still runs one live stream at a time at size %i', async (size) => {
    renderWall(size);
    const liveStreams = () =>
      Array.from(document.querySelectorAll('img')).filter((image) => image.src.includes('/camera/stream'));

    await waitFor(() => expect(screen.getByText('0 live, 3 snapshots, 3 total')).toBeInTheDocument());
    expect(liveStreams()).toHaveLength(0);

    fireEvent.click(screen.getByRole('button', { name: 'Watch X1C-A live' }));
    await waitFor(() => expect(screen.getByText('1 live, 2 snapshots, 3 total')).toBeInTheDocument());
    await waitFor(() => expect(liveStreams()).toHaveLength(1));

    // Picking another tile moves the one stream; it does not add a second.
    fireEvent.click(screen.getByRole('button', { name: 'Watch P1S-B live' }));
    await waitFor(() => expect(liveStreams().map((image) => image.src).join()).toContain('/printers/102/'));
    expect(liveStreams()).toHaveLength(1);
    expect(screen.getByText('1 live, 2 snapshots, 3 total')).toBeInTheDocument();
  });

  it('offers the size in its settings only to a wall that can change it', async () => {
    const first = renderWall(2);
    fireEvent.click(await screen.findByTitle('Cam wall settings'));
    expect(screen.queryByRole('group', { name: 'Tile size' })).not.toBeInTheDocument();
    first.unmount();

    const onChangeTileSize = vi.fn();
    renderWall(2, onChangeTileSize);
    fireEvent.click(await screen.findByTitle('Cam wall settings'));
    const sizes = screen.getByRole('group', { name: 'Tile size' });
    expect(sizes.querySelector('[aria-pressed="true"]')?.textContent).toBe('M');

    fireEvent.click(Array.from(sizes.querySelectorAll('button')).find((button) => button.textContent === 'XL')!);
    expect(onChangeTileSize).toHaveBeenCalledWith(4);
  });
});
