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
