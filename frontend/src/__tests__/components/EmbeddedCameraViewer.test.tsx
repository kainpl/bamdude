import { StrictMode } from 'react';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from '../utils';
import { EmbeddedCameraViewer } from '../../components/EmbeddedCameraViewer';
import { printerSource } from '../../utils/cameraSource';
import { holdsImage, stubBoxSize } from '../stubBoxSize';

describe('floating camera lifecycle', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(global, 'fetch').mockResolvedValue(new Response('{}'));
  });
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });
  const viewer = (id = 42) => <EmbeddedCameraViewer key={id} source={printerSource(id)} name={`Camera ${id}`} onClose={() => {}} />;
  const image = () => screen.getByAltText('Camera stream') as HTMLImageElement;
  const settle = () => act(async () => { await Promise.resolve(); });

  it('cancels the replacement stream after refresh then unmount, including StrictMode', async () => {
    const { unmount } = render(<StrictMode>{viewer()}</StrictMode>);
    await settle();
    const first = image();
    expect(first.src).toContain('/camera/stream?');
    fireEvent.load(first);
    fireEvent.click(screen.getByTitle('Refresh stream'));
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    const replacement = image();
    expect(replacement).not.toBe(first);
    expect(first.src).toMatch(/^data:image\/gif;/);
    expect(replacement.src).toContain('/camera/stream?');
    unmount();
    expect(replacement.src).toMatch(/^data:image\/gif;/);
  });

  it('cancels on minimize, restores on expand and releases the replacement on close', async () => {
    const { unmount } = render(viewer());
    await settle();
    const first = image();
    fireEvent.click(screen.getByTitle('Minimize'));
    expect(first.src).toMatch(/^data:image\/gif;/);
    expect(screen.queryByAltText('Camera stream')).toBeNull();
    fireEvent.click(screen.getByTitle('Expand'));
    const restored = image();
    expect(restored.src).toContain('/camera/stream?');
    unmount();
    expect(restored.src).toMatch(/^data:image\/gif;/);
  });

  it('cancels the previous printer when the single popup switches its key', async () => {
    const { rerender } = render(viewer(1));
    await settle();
    const old = image();
    rerender(viewer(2));
    await settle();
    expect(old.src).toMatch(/^data:image\/gif;/);
    expect(screen.getAllByAltText('Camera stream')).toHaveLength(1);
    expect(image().src).toContain('/printers/2/');
  });
});

// The window shares its camera's upstream with every other view of it. When the
// server ends the window's own viewer (its authority retired, its token
// expired) the upstream's status stays healthy, and the picture froze.
describe('floating camera whose own stream the server ended', () => {
  let statusReads: string[] = [];
  beforeEach(() => {
    vi.useFakeTimers();
    statusReads = [];
    vi.spyOn(global, 'fetch').mockImplementation(async (input) => {
      const url = String(input instanceof Request ? input.url : input);
      if (!/\/camera\/status/.test(url)) return new Response('{}');
      statusReads.push(url);
      return new Response(JSON.stringify({ active: true, stalled: false, viewer_attached: false }));
    });
  });
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });
  const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

  it('asks about the stream it opened, and reconnects when that one is gone', async () => {
    render(<EmbeddedCameraViewer source={printerSource(42)} name="H2C" onClose={() => {}} />);
    await advance(0);
    const first = screen.getByAltText('Camera stream') as HTMLImageElement;
    const viewer = new URL(first.src).searchParams.get('viewer');
    expect(viewer).toMatch(/^[A-Za-z0-9_-]{8,32}$/);
    fireEvent.load(first);

    let reconnecting = false;
    for (let read = 0; read < 2 && !reconnecting; read += 1) {
      await advance(5000);
      reconnecting = screen.queryByText(/Reconnecting in/) !== null;
    }

    expect(reconnecting).toBe(true);
    expect(statusReads.length).toBeGreaterThan(0);
    for (const url of statusReads) expect(new URL(url, 'http://x').searchParams.get('viewer')).toBe(viewer);
  });
});

// The default was the desktop one everywhere: wider than a phone and starting
// left of its screen, with the title bar inside the compact header's band.
describe('floating camera placement', () => {
  const viewer = () => <EmbeddedCameraViewer source={printerSource(7)} name="A1 Mini" onClose={() => {}} />;
  const frame = () => screen.getByText('A1 Mini').closest('.fixed') as HTMLElement;
  const px = (value: string) => parseFloat(value);
  const size = { width: window.innerWidth, height: window.innerHeight };
  const setScreen = (width: number, height: number) => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: width });
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: height });
  };

  beforeEach(() => {
    localStorage.clear();
    vi.spyOn(global, 'fetch').mockResolvedValue(new Response('{}'));
    setScreen(375, 667);
    document.documentElement.style.setProperty('--app-top', '3.5rem');
  });
  afterEach(() => {
    setScreen(size.width, size.height);
    document.documentElement.style.removeProperty('--app-top');
    vi.restoreAllMocks();
  });

  it('opens whole on a phone screen and below the compact header', async () => {
    render(viewer());
    await act(async () => { await Promise.resolve(); });

    const { left, top, width, height } = frame().style;
    expect(px(left)).toBeGreaterThanOrEqual(0);
    expect(px(left) + px(width)).toBeLessThanOrEqual(375);
    expect(px(top)).toBeGreaterThanOrEqual(56);
    expect(px(top) + px(height)).toBeLessThanOrEqual(667);
  });

  it('brings a window saved under the header, or off a smaller screen, back into reach', async () => {
    localStorage.setItem('embeddedCameraState_printer-7', JSON.stringify({ x: 1500, y: 5, width: 900, height: 700 }));
    render(viewer());
    await act(async () => { await Promise.resolve(); });

    const { left, top, width } = frame().style;
    expect(px(top)).toBe(56);
    expect(px(left)).toBeLessThanOrEqual(275);
    expect(px(width)).toBeLessThanOrEqual(375);
  });

  // Rotating a phone, or narrowing a desktop window across the compact-header
  // breakpoint, used to leave the title bar — the only handle the window is
  // dragged and closed by — off screen until the page was reloaded.
  it('comes back into reach when the viewport shrinks under it', async () => {
    setScreen(1440, 900);
    document.documentElement.style.setProperty('--app-top', '0px');
    render(viewer());
    await act(async () => { await Promise.resolve(); });
    expect(px(frame().style.left)).toBe(1020);

    setScreen(375, 667);
    act(() => { window.dispatchEvent(new Event('resize')); });

    const { left, top, width, height } = frame().style;
    expect(px(left)).toBeLessThanOrEqual(275);
    expect(px(width)).toBeLessThanOrEqual(375);
    expect(px(top) + 40).toBeLessThanOrEqual(667);
    expect(px(height)).toBeLessThanOrEqual(667);
  });

  it('comes back into reach when a phone is turned', async () => {
    setScreen(667, 375);
    localStorage.setItem('embeddedCameraState_printer-7', JSON.stringify({ x: 300, y: 60, width: 360, height: 300 }));
    render(viewer());
    await act(async () => { await Promise.resolve(); });
    expect(px(frame().style.left)).toBe(300);

    setScreen(375, 667);
    act(() => { window.dispatchEvent(new Event('orientationchange')); });

    expect(px(frame().style.left)).toBeLessThanOrEqual(275);
    expect(px(frame().style.width)).toBeLessThanOrEqual(375);
  });

  it('moves out from under the compact header when it appears', async () => {
    document.documentElement.style.setProperty('--app-top', '0px');
    localStorage.setItem('embeddedCameraState_printer-7', JSON.stringify({ x: 10, y: 5, width: 300, height: 200 }));
    render(viewer());
    await act(async () => { await Promise.resolve(); });
    expect(px(frame().style.top)).toBe(5);

    // Layout publishes the header's height after the resize event has been handled.
    await act(async () => {
      document.documentElement.style.setProperty('--app-top', '3.5rem');
      await Promise.resolve();
    });

    await waitFor(() => expect(px(frame().style.top)).toBe(56));
  });

  it('cannot be resized off the screen or left under the header', async () => {
    document.documentElement.style.setProperty('--app-top', '0px');
    localStorage.setItem('embeddedCameraState_printer-7', JSON.stringify({ x: 10, y: 20, width: 300, height: 200 }));
    render(viewer());
    await act(async () => { await Promise.resolve(); });
    // The header appears with nothing telling the window (no resize, no style
    // event it could act on yet): the resize itself must respect it.
    Object.defineProperty(document.documentElement.style, 'getPropertyValue', {
      configurable: true,
      value: (name: string) => (name === '--app-top' ? '3.5rem' : ''),
    });
    try {
      fireEvent.mouseDown(screen.getByTitle('Drag to resize'), { clientX: 310, clientY: 220 });
      fireEvent.mouseMove(document, { clientX: 5000, clientY: 5000 });
      fireEvent.mouseUp(document);

      const { left, top, width, height } = frame().style;
      expect(px(top)).toBe(56);
      expect(px(left) + px(width)).toBeLessThanOrEqual(375);
      expect(px(top) + px(height)).toBeLessThanOrEqual(667);
    } finally {
      delete (document.documentElement.style as unknown as { getPropertyValue?: unknown }).getPropertyValue;
    }
  });

  it('cannot be dragged under the header', async () => {
    render(viewer());
    await act(async () => { await Promise.resolve(); });

    fireEvent.mouseDown(screen.getByText('A1 Mini'), { clientX: 100, clientY: 100 });
    fireEvent.mouseMove(document, { clientX: 100, clientY: -300 });
    fireEvent.mouseUp(document);
    expect(px(frame().style.top)).toBe(56);
  });
});

// A sideways camera was laid out at the window's unswapped size and then
// turned: cut off in a wide window, boxed in by black bars in a tall one.
describe('floating camera turned sideways', () => {
  let restoreBox: () => void = () => {};
  const withRotation = (rotation: number) => {
    vi.spyOn(global, 'fetch').mockImplementation(async (input) => (
      /\/printers\/7$/.test(String(input instanceof Request ? input.url : input))
        ? new Response(JSON.stringify({ id: 7, name: 'A1 Mini', camera_rotation: rotation }))
        : new Response('{}')
    ));
  };
  const viewer = () => <EmbeddedCameraViewer source={printerSource(7)} name="A1 Mini" onClose={() => {}} />;

  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { restoreBox(); vi.restoreAllMocks(); });

  it.each([
    [270, 400, 260, { maxWidth: '260px', maxHeight: '400px' }],
    [90, 260, 400, { maxWidth: '400px', maxHeight: '260px' }],
  ])('fits a camera turned %i degrees into a %ix%i video area', async (rotation, width, height, expected) => {
    restoreBox = stubBoxSize(holdsImage('Camera stream'), width, height);
    withRotation(rotation);

    render(viewer());

    const img = await screen.findByAltText('Camera stream');
    await waitFor(() => expect(img.style.transform).toContain(`rotate(${rotation}deg)`));
    await waitFor(() => expect(img).toHaveStyle(expected));
    // Zoom and pan still own the transform.
    expect(img.style.transform).toContain('scale(1)');
  });

  it('still zooms and pans a sideways camera, within the same limits', async () => {
    restoreBox = stubBoxSize(holdsImage('Camera stream'), 400, 260);
    withRotation(90);

    render(viewer());

    const img = await screen.findByAltText('Camera stream');
    await waitFor(() => expect(img).toHaveStyle({ maxWidth: '260px', maxHeight: '400px' }));

    fireEvent.click(screen.getByTitle('Zoom in'));
    expect(img.style.transform).toContain('scale(1.5)');
    fireEvent.mouseDown(img, { clientX: 100, clientY: 100 });
    fireEvent.mouseMove(img, { clientX: 130, clientY: 120 });
    fireEvent.mouseUp(img);
    expect(img.style.transform).toBe('scale(1.5) translate(20px, 13.333333333333334px) rotate(90deg)');
    expect(img).toHaveStyle({ maxWidth: '260px', maxHeight: '400px' });
  });

  it('leaves an upside-down camera at the window size', async () => {
    restoreBox = stubBoxSize(holdsImage('Camera stream'), 400, 260);
    withRotation(180);

    render(viewer());

    const img = await screen.findByAltText('Camera stream');
    await waitFor(() => expect(img.style.transform).toContain('rotate(180deg)'));
    expect(img.style.maxWidth).toBe('');
    expect(img.style.maxHeight).toBe('');
  });
});
