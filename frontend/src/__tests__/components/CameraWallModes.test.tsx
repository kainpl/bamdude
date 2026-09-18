import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { CameraWall, type CameraWallStatus } from '../../components/CameraWall';

const printers = [
  { id: 101, name: 'X1C-A' },
  { id: 102, name: 'P1S-B' },
];
let fetchMock: ReturnType<typeof vi.fn>;

function connectedStatuses() {
  return new Map<number, CameraWallStatus>([
    [101, { connected: true, state: 'RUNNING' }],
    [102, { connected: true, state: 'IDLE' }],
  ]);
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

describe('CameraWall live selection', () => {
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', VisibleIntersectionObserver);
    fetchMock = vi.fn().mockResolvedValue(new Response('jpeg', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
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

  function renderWall(onOpenPrinterCard = vi.fn()) {
    return render(
      <CameraWall
        printers={printers}
        snapshotIntervalSec={10}
        statusMode="compact"
        statusOverride={connectedStatuses()}
        onOpenPrinterCard={onOpenPrinterCard}
        onChangeSnapshotIntervalSec={vi.fn()}
        onChangeStatusMode={vi.fn()}
      />,
    );
  }

  it('starts with snapshots and only creates a live view after an explicit tile click', async () => {
    renderWall();

    await waitFor(() => expect(screen.getByText('0 live, 2 snapshots, 2 total')).toBeInTheDocument());
    expect(screen.queryByText('Live')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Watch X1C-A live' }));
    await waitFor(() => {
      const image = screen.getByAltText('X1C-A') as HTMLImageElement;
      expect(image.src).toContain('/api/v1/printers/101/camera/stream');
    });
    expect(screen.getByText('1 live, 1 snapshots, 2 total')).toBeInTheDocument();
  });

  it('moves the only live stream to the next selected tile and releases the previous one', async () => {
    renderWall();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Watch X1C-A live' })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Watch X1C-A live' }));
    await waitFor(() => expect((screen.getByAltText('X1C-A') as HTMLImageElement).src).toContain('/camera/stream'));

    fireEvent.click(screen.getByRole('button', { name: 'Watch P1S-B live' }));
    await waitFor(() => expect((screen.getByAltText('P1S-B') as HTMLImageElement).src).toContain('/camera/stream'));
    await waitFor(() => {
      const stopCalls = fetchMock.mock.calls.filter(([url]) =>
        String(url).includes('/api/v1/printers/101/camera/stop'),
      );
      expect(stopCalls).toHaveLength(1);
    });
    expect(screen.getByText('1 live, 1 snapshots, 2 total')).toBeInTheDocument();
  });

  it('opens the printer card without selecting a live stream', async () => {
    const onOpenPrinterCard = vi.fn();
    renderWall(onOpenPrinterCard);
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Open printer card' })).toHaveLength(2));

    fireEvent.click(screen.getAllByRole('button', { name: 'Open printer card' })[0]);
    expect(onOpenPrinterCard).toHaveBeenCalledWith(101, 'X1C-A');
    expect(screen.getByText('0 live, 2 snapshots, 2 total')).toBeInTheDocument();
  });

  it('keeps a token-style passive wall free of live and printer-card controls', async () => {
    render(
      <CameraWall
        printers={printers}
        snapshotIntervalSec={10}
        statusMode="compact"
        statusOverride={connectedStatuses()}
        onChangeSnapshotIntervalSec={vi.fn()}
        onChangeStatusMode={vi.fn()}
        hideSettings
      />,
    );

    await waitFor(() => expect(screen.getByText('0 live, 2 snapshots, 2 total')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Watch X1C-A live' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Open printer card' })).not.toBeInTheDocument();
  });
});

describe('CameraWall with cameras that belong to no printer', () => {
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', VisibleIntersectionObserver);
    fetchMock = vi.fn().mockResolvedValue(new Response('jpeg', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
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

  const cameras = [
    { id: 4, name: 'Shelf', rotation: 0 },
    { id: 5, name: 'Lobby', rotation: 90 },
  ];

  function renderWallWithCameras() {
    return render(
      <CameraWall
        printers={printers}
        cameras={cameras}
        snapshotIntervalSec={10}
        statusMode="compact"
        statusOverride={connectedStatuses()}
        onOpenPrinterCard={vi.fn()}
        onChangeSnapshotIntervalSec={vi.fn()}
        onChangeStatusMode={vi.fn()}
      />,
    );
  }

  it('draws the cameras after the printers and counts them in the wall', async () => {
    renderWallWithCameras();

    await waitFor(() => expect(screen.getByText('0 live, 4 snapshots, 4 total')).toBeInTheDocument());
    const labels = screen.getAllByTitle(/X1C-A|P1S-B|Shelf|Lobby/).map((el) => el.getAttribute('title'));
    expect(labels).toEqual(['X1C-A', 'P1S-B', 'Lobby', 'Shelf']);
  });

  it('gives a camera the one live slot, from its own route', async () => {
    renderWallWithCameras();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Watch Shelf live' })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Watch Shelf live' }));
    await waitFor(() => {
      const image = screen.getByAltText('Shelf') as HTMLImageElement;
      expect(image.src).toContain('/api/v1/cameras/4/stream');
    });
    expect(screen.getByText('1 live, 3 snapshots, 4 total')).toBeInTheDocument();
  });

  it('offers no printer card for a camera — there is none to open', async () => {
    renderWallWithCameras();
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Open printer card' })).toHaveLength(2));
  });
});
