/**
 * Camera wall tiles carry what a printer card shows at a glance (upstream #3479): the
 * heaters and the finish time in the full overlay, and pause/resume, stop and the
 * chamber light for a user allowed to control printers — with the card's confirmations,
 * through the card's API calls. None for an offline printer or a kiosk (token) wall.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { CameraTile } from '../../components/CameraTile';
import { CameraWall, type CameraWallStatus } from '../../components/CameraWall';
import { printerSource } from '../../utils/cameraSource';

let canControl = true;
vi.mock('../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => ({
      hasPermission: (p: string) => (p === 'printers:control' ? canControl : true),
      hasAnyPermission: () => true,
      authEnabled: true,
      user: null,
      loading: false,
    }),
  };
});

const PRINTER = { id: 5, name: 'Farm-5' };
const PRINTING: CameraWallStatus = {
  connected: true,
  state: 'RUNNING',
  progress: 40,
  remaining_time: 90,
  temperatures: { nozzle: 219.6, bed: 60, chamber: 34.4 },
  chamber_light: false,
};

let posted: string[];

function serveStatus(status: CameraWallStatus) {
  server.use(http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ id: PRINTER.id, ...status })));
}

beforeEach(() => {
  canControl = true;
  posted = [];
  server.use(
    http.post('/api/v1/printers/:id/print/:action', ({ params }) => {
      posted.push(`${params.action} ${params.id}`);
      return HttpResponse.json({ success: true, message: 'ok' });
    }),
    http.post('/api/v1/printers/:id/chamber-light', ({ request }) => {
      posted.push(`light ${new URL(request.url).searchParams.get('on')}`);
      return HttpResponse.json({ success: true, message: 'ok' });
    }),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('tile information (full overlay)', () => {
  function tile(props: Partial<Parameters<typeof CameraTile>[0]>) {
    return render(
      <CameraTile
        source={printerSource(5)}
        name="Farm-5"
        mode="paused"
        snapshotIntervalMs={8000}
        connected
        statusMode="full"
        printerState="RUNNING"
        remainingMin={90}
        temperatures={{ nozzle: 219.6, nozzle_2: 180, bed: 60, chamber: 34.4 }}
        timeFormat="24h"
        {...props}
      />,
    );
  }

  it('shows every heater, both nozzles of a dual-nozzle printer, rounded', () => {
    tile({});
    expect(screen.getByTitle('Nozzle')).toHaveTextContent('220° / 180°');
    expect(screen.getByTitle('Bed')).toHaveTextContent('60°');
    expect(screen.getByTitle('Chamber')).toHaveTextContent('34°');
  });

  it('leaves out a heater the printer does not report', () => {
    tile({ temperatures: { nozzle: 200, bed: 55 } });
    expect(screen.queryByTitle('Chamber')).not.toBeInTheDocument();
    expect(screen.getByTitle('Bed')).toHaveTextContent('55°');
  });

  it('adds the finish time beside the time left while printing', () => {
    tile({});
    expect(screen.getByText('1h 30m left')).toBeInTheDocument();
    expect(screen.getByText(/^ETA /)).toBeInTheDocument();
  });

  it('has no finish time on a paused print: it moves with the pause', () => {
    tile({ printerState: 'PAUSE' });
    expect(screen.getByText('1h 30m left')).toBeInTheDocument();
    expect(screen.queryByText(/^ETA /)).not.toBeInTheDocument();
  });

  it('shows no heaters in the compact overlay or for an offline printer', () => {
    const { unmount } = tile({ statusMode: 'compact' });
    expect(screen.queryByTitle('Nozzle')).not.toBeInTheDocument();
    unmount();
    tile({ connected: false });
    expect(screen.queryByTitle('Nozzle')).not.toBeInTheDocument();
  });
});

describe('tile controls', () => {
  function renderWall(props: Partial<Parameters<typeof CameraWall>[0]> = {}) {
    return render(
      <CameraWall
        printers={[PRINTER]}
        snapshotIntervalSec={8}
        statusMode="full"
        onOpenPrinterCard={vi.fn()}
        onChangeSnapshotIntervalSec={vi.fn()}
        onChangeStatusMode={vi.fn()}
        showControls
        onChangeControlsMode={vi.fn()}
        timeFormat="24h"
        {...props}
      />,
    );
  }

  it('stops a print only after the same confirmation as the card', async () => {
    serveStatus(PRINTING);
    renderWall();

    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/stop the current print on "Farm-5"/)).toBeInTheDocument();
    expect(posted).toEqual([]);

    fireEvent.click(within(dialog).getByRole('button', { name: 'Stop Print' }));
    await waitFor(() => expect(posted).toEqual(['stop 5']));
  });

  it('pauses a running print and resumes a paused one, each confirmed', async () => {
    serveStatus(PRINTING);
    const { unmount } = renderWall();
    fireEvent.click(await screen.findByRole('button', { name: 'Pause' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Pause Print' }));
    await waitFor(() => expect(posted).toEqual(['pause 5']));
    unmount();

    serveStatus({ ...PRINTING, state: 'PAUSE' });
    renderWall();
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Resume Print' }));
    await waitFor(() => expect(posted).toEqual(['pause 5', 'resume 5']));
  });

  it('switches the chamber light', async () => {
    serveStatus(PRINTING);
    renderWall();

    fireEvent.click(await screen.findByRole('button', { name: 'Turn on chamber light' }));
    await waitFor(() => expect(posted).toEqual(['light true']));
  });

  it('offers only the light while the printer does not print', async () => {
    serveStatus({ ...PRINTING, state: 'IDLE' });
    renderWall();

    expect(await screen.findByRole('button', { name: 'Turn on chamber light' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
  });

  it('gives a user without printers:control no controls', async () => {
    canControl = false;
    serveStatus(PRINTING);
    renderWall();

    // The overlay arrived, so the status did: still no buttons.
    expect(await screen.findByTitle('Nozzle')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Turn on chamber light' })).not.toBeInTheDocument();
  });

  it('gives an offline printer no controls', async () => {
    serveStatus({ ...PRINTING, connected: false });
    renderWall();

    await waitFor(() => expect(screen.getByText('0 live, 0 snapshots, 1 total')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Turn on chamber light' })).not.toBeInTheDocument();
  });

  it('never gives a kiosk wall controls, even when its page asks', () => {
    renderWall({ statusOverride: new Map([[PRINTER.id, PRINTING]]), onOpenPrinterCard: undefined });

    expect(screen.queryByTestId('camwall-tile-controls')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
  });

  it('waits for hover unless the wall shows them always', async () => {
    serveStatus(PRINTING);
    const { unmount } = renderWall({ controlsMode: 'hover' });
    await screen.findByRole('button', { name: 'Stop' });
    expect(screen.getByTestId('camwall-tile-controls').className).toContain('opacity-0');
    unmount();

    renderWall({ controlsMode: 'always' });
    await screen.findByRole('button', { name: 'Stop' });
    expect(screen.getByTestId('camwall-tile-controls').className).not.toContain('opacity-0');
  });

  it('offers the controls setting only on a wall that has the controls', async () => {
    serveStatus(PRINTING);
    const onChangeControlsMode = vi.fn();
    const { unmount } = renderWall({ onChangeControlsMode });
    fireEvent.click(screen.getByTitle('Cam wall settings'));
    const group = await screen.findByRole('radiogroup', { name: 'Tile controls' });
    fireEvent.click(within(group).getByRole('radio', { name: 'Always' }));
    expect(onChangeControlsMode).toHaveBeenCalledWith('always');
    unmount();

    renderWall({ showControls: false });
    fireEvent.click(screen.getByTitle('Cam wall settings'));
    expect(screen.queryByRole('radiogroup', { name: 'Tile controls' })).not.toBeInTheDocument();
  });
});
