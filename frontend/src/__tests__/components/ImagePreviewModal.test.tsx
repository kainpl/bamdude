/**
 * The image viewer: a picture in the lightbox shell, with zoom and pan.
 *
 * jsdom draws nothing, so the picture's geometry is read off its inline
 * transform — the one thing the viewer computes.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, screen } from '@testing-library/react';
import { render } from '../utils';
import { ImagePreviewModal, MAX_IMAGE_BYTES } from '../../components/ImagePreviewModal';
import i18n from '../../i18n';

const SRC = '/api/v1/library/files/7/image';

function show(props: Partial<Parameters<typeof ImagePreviewModal>[0]> = {}) {
  const onClose = vi.fn();
  render(<ImagePreviewModal src={SRC} title="photo.png" fileSize={1024} onClose={onClose} {...props} />);
  return { onClose };
}

const picture = () => screen.getByRole('img', { name: 'photo.png' }) as HTMLImageElement;
const stage = () => screen.getByTestId('image-stage');
/** `translate(xpx, ypx) scale(s)` → numbers. */
function view() {
  const m = /translate\((-?[\d.]+)px, (-?[\d.]+)px\) scale\(([\d.]+)\)/.exec(picture().style.transform);
  if (!m) throw new Error(`no transform: "${picture().style.transform}"`);
  return { x: Number(m[1]), y: Number(m[2]), scale: Number(m[3]) };
}

/** A pointer event jsdom can carry: it has no PointerEvent of its own. */
function pointer(type: string, id: number, clientX: number, clientY: number) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX, clientY });
  Object.defineProperty(event, 'pointerId', { value: id });
  act(() => {
    stage().dispatchEvent(event);
  });
}

/** The fitted picture fills an 800×600 stage (jsdom measures everything as 0). */
function stubGeometry() {
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) {
    return this.dataset.testid === 'image-stage' ? 800 : 0;
  });
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return this.dataset.testid === 'image-stage' ? 600 : 0;
  });
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockImplementation(function (this: HTMLElement) {
    return this.tagName === 'IMG' ? 800 : 0;
  });
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return this.tagName === 'IMG' ? 600 : 0;
  });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return { left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, x: 0, y: 0, toJSON: () => ({}) };
  });
}

describe('ImagePreviewModal', () => {
  beforeEach(() => {
    stubGeometry();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows the picture from the URL it is given, as a dialog named after the file', () => {
    show();

    expect(screen.getByRole('dialog', { name: 'photo.png' })).toBeInTheDocument();
    expect(picture().getAttribute('src')).toBe(SRC);
    expect(view()).toEqual({ x: 0, y: 0, scale: 1 });
  });

  it('zooms with the buttons and comes back with reset', () => {
    show();

    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    expect(view().scale).toBeGreaterThan(1.4);
    expect(screen.getByRole('button', { name: 'Reset zoom' })).toHaveTextContent(`${Math.round(view().scale * 100)}%`);

    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    expect(view().scale).toBeGreaterThan(1);

    fireEvent.click(screen.getByRole('button', { name: 'Reset zoom' }));
    expect(view()).toEqual({ x: 0, y: 0, scale: 1 });
  });

  it('never zooms out below the fitted picture', () => {
    show();

    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    expect(view().scale).toBe(1);
    expect(screen.getByRole('button', { name: 'Zoom out' })).toBeDisabled();
  });

  it('zooms with + and −, resets with 0 — and leaves the browser its own Ctrl shortcuts', () => {
    show();

    fireEvent.keyDown(window, { key: '+' });
    fireEvent.keyDown(window, { key: '=' });
    const zoomed = view().scale;
    expect(zoomed).toBeGreaterThan(1.4);

    fireEvent.keyDown(window, { key: '-' });
    expect(view().scale).toBeLessThan(zoomed);

    fireEvent.keyDown(window, { key: '+', ctrlKey: true });
    expect(view().scale).toBeLessThan(zoomed);

    fireEvent.keyDown(window, { key: '0' });
    expect(view().scale).toBe(1);
  });

  it('zooms with the wheel, towards the pointer', () => {
    show();

    // The stage is 800×600; the pointer sits right of and below its centre.
    fireEvent.wheel(stage(), { deltaY: -100, clientX: 600, clientY: 400 });
    const after = view();
    expect(after.scale).toBeGreaterThan(1);
    // The point under the pointer stays put, so the picture moves away from it.
    expect(after.x).toBeLessThan(0);
    expect(after.y).toBeLessThan(0);

    fireEvent.wheel(stage(), { deltaY: 100, clientX: 600, clientY: 400 });
    fireEvent.wheel(stage(), { deltaY: 100, clientX: 600, clientY: 400 });
    expect(view()).toEqual({ x: 0, y: 0, scale: 1 });
  });

  it('pans a zoomed picture by dragging, and only as far as its edge', () => {
    show();
    for (let i = 0; i < 6; i += 1) fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    const { scale } = view();
    // 800 px fitted × scale, on an 800 px stage: this far each way, no further.
    const limit = (800 * scale - 800) / 2;
    expect(limit).toBeGreaterThan(50);

    pointer('pointerdown', 1, 300, 300);
    pointer('pointermove', 1, 340, 300);
    expect(view().x).toBe(40);

    pointer('pointermove', 1, 5000, 300);
    expect(view().x).toBeCloseTo(limit, 3);
    pointer('pointerup', 1, 5000, 300);
  });

  it('does not move a picture that is not zoomed', () => {
    show();

    pointer('pointerdown', 1, 300, 300);
    pointer('pointermove', 1, 380, 360);
    pointer('pointerup', 1, 380, 360);

    expect(view()).toEqual({ x: 0, y: 0, scale: 1 });
  });

  it('zooms with a pinch', () => {
    show();

    pointer('pointerdown', 1, 350, 300);
    pointer('pointerdown', 2, 450, 300);
    pointer('pointermove', 2, 550, 300); // 100 px apart → 200 px apart
    expect(view().scale).toBeCloseTo(2, 3);

    pointer('pointerup', 1, 350, 300);
    pointer('pointerup', 2, 550, 300);
  });

  it('closes on Escape, on its X and on a click on the ground — not on a click on the picture or after a drag', () => {
    const { onClose } = show();

    fireEvent.click(picture());
    expect(onClose).not.toHaveBeenCalled();

    // A drag that ends over the ground is the end of a drag, not a click.
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    pointer('pointerdown', 1, 300, 300);
    pointer('pointermove', 1, 330, 300);
    pointer('pointerup', 1, 330, 300);
    fireEvent.click(stage());
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(stage());
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalledTimes(2);

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  // A real browser hands the click of a captured pointer to the capturing
  // element — the stage — whatever was pressed. jsdom does not, so the test
  // dispatches what the browser would: the press on the picture, the click on
  // the stage.
  it('does not close on a press and release on the picture, although the browser reports the click on the stage', () => {
    const { onClose } = show();
    const press = (type: string) => {
      const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: 400, clientY: 300 });
      Object.defineProperty(event, 'pointerId', { value: 1 });
      act(() => {
        picture().dispatchEvent(event);
      });
    };

    press('pointerdown');
    pointer('pointerup', 1, 400, 300);
    fireEvent.click(stage());
    expect(onClose).not.toHaveBeenCalled();

    // The same at 400 %: the zoom survives the click.
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    const zoomed = view().scale;
    press('pointerdown');
    pointer('pointerup', 1, 400, 300);
    fireEvent.click(stage());
    expect(onClose).not.toHaveBeenCalled();
    expect(view().scale).toBe(zoomed);

    // A press and release on the ground still closes.
    pointer('pointerdown', 1, 5, 5);
    pointer('pointerup', 1, 5, 5);
    fireEvent.click(stage());
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('says so, in the interface language, when the picture does not load', async () => {
    await act(async () => {
      await i18n.changeLanguage('uk');
    });
    try {
      show();
      fireEvent.error(picture());

      expect(screen.getByText('Не вдалося завантажити зображення.')).toBeInTheDocument();
      expect(screen.queryByRole('img')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Збільшити' })).not.toBeInTheDocument();
    } finally {
      await act(async () => {
        await i18n.changeLanguage('en');
      });
    }
  });

  it('does not even ask for a file too large to decode in a tab', () => {
    show({ fileSize: MAX_IMAGE_BYTES + 1 });

    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByText(/too large to preview/i)).toBeInTheDocument();
  });

  it('drops a picture whose pixel count would exhaust the tab', () => {
    show();
    const img = picture();
    Object.defineProperty(img, 'naturalWidth', { value: 20000 });
    Object.defineProperty(img, 'naturalHeight', { value: 20000 });
    fireEvent.load(img);

    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByText(/too large to preview/i)).toBeInTheDocument();
  });
});
