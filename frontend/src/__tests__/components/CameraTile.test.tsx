import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { StrictMode } from 'react';
import { act, fireEvent, screen } from '@testing-library/react';
import { render } from '../utils';
import { CameraTile } from '../../components/CameraTile';
import { printerSource } from '../../utils/cameraSource';
import { ConnectionProvider, useConnection } from '../../contexts/ConnectionContext';

function ServerConnectionToggle() {
  const { setIsConnected } = useConnection();
  return <>
    <button onClick={() => setIsConnected(false)}>Disconnect server</button>
    <button onClick={() => setIsConnected(true)}>Reconnect server</button>
  </>;
}

// The shared render() util mounts AuthProvider, which fires an async
// /auth/me probe on mount. Each test absorbs that settle with a single
// `await act(async () => {})` after render so the AuthProvider state
// update doesn't bleed into the assertion phase as an act() warning.
async function flushMicrotasks() {
  await act(async () => {
    await Promise.resolve();
  });
}

describe('CameraTile', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(global, 'fetch').mockImplementation(async () => new Response('jpeg', { status: 200 }));
    let nextUrl = 0;
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = vi.fn(() => `blob:frame-${++nextUrl}`);
      static revokeObjectURL = vi.fn();
    });
    Object.defineProperty(HTMLImageElement.prototype, 'decode', {
      configurable: true, value: vi.fn().mockResolvedValue(undefined),
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('renders the live stream URL in live mode', async () => {
    render(
      <CameraTile
        source={printerSource(42)}
        name="X1C-Lab"
        mode="live"
        snapshotIntervalMs={5000}
        connected
      />,
    );
    await flushMicrotasks();
    const img = screen.getByAltText('X1C-Lab') as HTMLImageElement;
    expect(img.src).toContain('/api/v1/printers/42/camera/stream');
    expect(img.src).toContain('fps=8');
  });

  it('marks a previously loaded live frame stale while the server is down and reloads it on reconnect', async () => {
    render(<ConnectionProvider>
      <CameraTile source={printerSource(42)} name="Live" mode="live" snapshotIntervalMs={5000} connected />
      <ServerConnectionToggle />
    </ConnectionProvider>);
    await flushMicrotasks();
    const image = screen.getByAltText('Live') as HTMLImageElement;
    fireEvent.load(image);
    const before = image.src;

    fireEvent.click(screen.getByText('Disconnect server'));
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByRole('status')).toHaveTextContent('Reconnecting');
    // A decoded MJPEG image may never fire onerror after the connection drops.
    expect(screen.getByAltText('Live')).toBe(image);

    fireEvent.click(screen.getByText('Reconnect server'));
    expect(screen.queryByRole('status')).toBeNull();
    expect((screen.getByAltText('Live') as HTMLImageElement).src).not.toBe(before);
  });

  it('displays completed snapshots and refreshes without replacing the image element', async () => {
    render(
      <CameraTile
        source={printerSource(7)}
        name="P1S-Garage"
        mode="snapshot"
        snapshotIntervalMs={1000}
        connected
      />,
    );
    await flushMicrotasks();
    const image = screen.getByAltText('P1S-Garage') as HTMLImageElement;
    const initial = image.src;
    expect(initial).toContain('blob:frame-');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    const refreshed = (screen.getByAltText('P1S-Garage') as HTMLImageElement).src;
    expect(screen.getByAltText('P1S-Garage')).toBe(image);
    expect(refreshed).toContain('blob:frame-');
    expect(refreshed).not.toBe(initial);
  });

  it('shows an offline placeholder when not connected', async () => {
    render(
      <CameraTile
        source={printerSource(1)}
        name="A1-Offline"
        mode="live"
        snapshotIntervalMs={5000}
        connected={false}
      />,
    );
    await flushMicrotasks();
    expect(screen.queryByAltText('A1-Offline')).toBeNull();
  });

  it('cancels the detached MJPEG image on navigation and restores it during Strict Mode replay', async () => {
    const { unmount } = render(<StrictMode><CameraTile
      source={printerSource(42)} name="Live" mode="live" snapshotIntervalMs={5000} connected
    /></StrictMode>);
    await flushMicrotasks();
    const image = screen.getByAltText('Live') as HTMLImageElement;
    expect(image.src).toContain('/camera/stream?');
    unmount();
    expect(image.src).toMatch(/^data:image\/gif;/);
  });

  it('cancels the old live image when switching to snapshots or going offline', async () => {
    const { rerender } = render(<CameraTile
      source={printerSource(42)} name="Live" mode="live" snapshotIntervalMs={5000} connected
    />);
    await flushMicrotasks();
    const first = screen.getByAltText('Live') as HTMLImageElement;
    rerender(<CameraTile source={printerSource(42)} name="Live" mode="snapshot" snapshotIntervalMs={5000} connected />);
    await flushMicrotasks();
    expect(first.src).toMatch(/^data:image\/gif;/);
    rerender(<CameraTile source={printerSource(42)} name="Live" mode="live" snapshotIntervalMs={5000} connected />);
    await flushMicrotasks();
    const second = screen.getByAltText('Live') as HTMLImageElement;
    rerender(<CameraTile source={printerSource(42)} name="Live" mode="live" snapshotIntervalMs={5000} connected={false} />);
    await flushMicrotasks();
    expect(second.src).toMatch(/^data:image\/gif;/);
  });

  it('shows the paused placeholder in paused mode', async () => {
    render(
      <CameraTile
        source={printerSource(9)}
        name="H2D-Booth"
        mode="paused"
        snapshotIntervalMs={5000}
        connected
      />,
    );
    await flushMicrotasks();
    expect(screen.queryByAltText('H2D-Booth')).toBeNull();
  });

  it('POSTs /camera/stop when leaving live mode', async () => {
    const fetchMock = vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(null, { status: 200 }),
    );
    const { rerender } = render(
      <CameraTile
        source={printerSource(11)}
        name="X1C-Stop"
        mode="live"
        snapshotIntervalMs={5000}
        connected
      />,
    );
    await flushMicrotasks();
    fetchMock.mockClear();

    await act(async () => {
      rerender(
        <CameraTile
          source={printerSource(11)}
          name="X1C-Stop"
          mode="snapshot"
          snapshotIntervalMs={5000}
          connected
        />,
      );
    });

    const stopCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes('/api/v1/printers/11/camera/stop'),
    );
    expect(stopCalls.length).toBeGreaterThan(0);
  });

  it('keeps live selection and the printer-card action separate', async () => {
    const onToggleLive = vi.fn();
    const onOpenPrinterCard = vi.fn();
    render(
      <CameraTile
        source={printerSource(23)}
        name="P1S-Detail"
        mode="snapshot"
        snapshotIntervalMs={5000}
        connected
        onToggleLive={onToggleLive}
        onOpenPrinterCard={onOpenPrinterCard}
      />,
    );
    await flushMicrotasks();

    fireEvent.click(screen.getByRole('button', { name: 'Watch P1S-Detail live' }));
    expect(onToggleLive).toHaveBeenCalledOnce();
    expect(onOpenPrinterCard).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Open printer card' }));
    expect(onOpenPrinterCard).toHaveBeenCalledOnce();
    expect(onToggleLive).toHaveBeenCalledOnce();
  });

  it('keeps pause and failure visible when ordinary overlays are off', async () => {
    const { rerender, container } = render(
      <CameraTile
        source={printerSource(24)}
        name="P1S-Attention"
        mode="snapshot"
        snapshotIntervalMs={5000}
        connected
        statusMode="off"
        printerState="PAUSE"
      />,
    );
    await flushMicrotasks();
    expect(screen.getByText('Paused')).toBeInTheDocument();
    expect(container.firstElementChild).toHaveClass('border-amber-400');

    rerender(
      <CameraTile
        source={printerSource(24)}
        name="P1S-Attention"
        mode="snapshot"
        snapshotIntervalMs={5000}
        connected
        statusMode="off"
        printerState="FAILED"
      />,
    );
    await flushMicrotasks();
    expect(screen.getByText('Error')).toBeInTheDocument();
    expect(container.firstElementChild).toHaveClass('border-red-500');
  });
});
