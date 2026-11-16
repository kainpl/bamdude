/**
 * Real fullscreen (the Fullscreen API) in the viewers.
 *
 * jsdom has no Fullscreen API, which is also what an iPhone looks like to the
 * page — so "no API" is the default here and the API is a stub the tests turn
 * on. The stub does what a browser does: it sets `document.fullscreenElement`
 * and fires `fullscreenchange`.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ImagePreviewModal } from '../../components/ImagePreviewModal';
import { ModelViewerModal } from '../../components/ModelViewerModal';
import { Modal } from '../../components/Modal';
import { useToast } from '../../contexts/ToastContext';
import { ESC_GRACE_MS } from '../../hooks/useElementFullscreen';

vi.mock('../../components/ModelViewer', () => ({
  ModelViewer: () => <div data-testid="model-viewer">Model Viewer Mock</div>,
}));
vi.mock('../../components/GcodeViewer', () => ({
  GcodeViewer: () => <div data-testid="gcode-viewer">G-code Viewer Mock</div>,
}));

interface FullscreenStub {
  request: ReturnType<typeof vi.fn>;
  exit: ReturnType<typeof vi.fn>;
  element: () => Element | null;
  /** The browser leaving fullscreen by itself (its own Escape handling). */
  browserLeaves: () => void;
}

function stubFullscreen(): FullscreenStub {
  let current: Element | null = null;
  const change = (next: Element | null) => {
    current = next;
    act(() => {
      document.dispatchEvent(new Event('fullscreenchange'));
    });
  };
  const request = vi.fn(function (this: Element) {
    change(this);
    return Promise.resolve();
  });
  const exit = vi.fn(() => {
    change(null);
    return Promise.resolve();
  });
  Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: true });
  Object.defineProperty(document, 'fullscreenElement', { configurable: true, get: () => current });
  Object.defineProperty(document, 'exitFullscreen', { configurable: true, value: exit });
  Object.defineProperty(Element.prototype, 'requestFullscreen', { configurable: true, value: request });
  return { request, exit, element: () => current, browserLeaves: () => change(null) };
}

function removeFullscreenStub() {
  for (const name of ['fullscreenEnabled', 'fullscreenElement', 'exitFullscreen'] as const) {
    delete (document as unknown as Record<string, unknown>)[name];
  }
  delete (Element.prototype as unknown as Record<string, unknown>).requestFullscreen;
}

/** Escape as a browser delivers it: to the focused element, bubbling up to the window. */
const pressEscape = () => fireEvent.keyDown(document.body, { key: 'Escape' });

let clock = 1000;

beforeEach(() => {
  clock = 1000;
  vi.spyOn(performance, 'now').mockImplementation(() => clock);
});

afterEach(() => {
  removeFullscreenStub();
  vi.restoreAllMocks();
});

/** Says something by toast when asked — as a failed download does. */
function Speaker() {
  const { showToast } = useToast();
  return (
    <button type="button" onClick={() => showToast('Download failed', 'error')}>
      speak
    </button>
  );
}

// What fullscreen shows is the fullscreen element's own subtree — nothing
// portalled into `body` is on screen in it.
describe('what the rest of the app shows while a viewer is in fullscreen', () => {
  const viewer = <ImagePreviewModal src="/api/v1/library/files/7/image" title="photo.png" fileSize={10} onClose={vi.fn()} />;

  it('leaves fullscreen when another dialog opens, so the box is never on screen and dead', () => {
    const fs = stubFullscreen();
    const view = render(viewer);
    fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }));
    expect(fs.element()).not.toBeNull();

    // An app-level dialog: a session prompt, a socket-driven question.
    view.rerender(
      <>
        {viewer}
        <Modal onClose={vi.fn()} title="Session expired">
          <p>Sign in again.</p>
        </Modal>
      </>,
    );

    expect(screen.getByRole('dialog', { name: 'Session expired' })).toBeInTheDocument();
    expect(fs.exit).toHaveBeenCalledTimes(1);
    expect(fs.element()).toBeNull();
  });

  it('keeps a fullscreen that already shows the whole page, dialogs included', () => {
    const fs = stubFullscreen();
    // The Monitor's fullscreen is the document itself.
    act(() => {
      void document.documentElement.requestFullscreen();
    });
    expect(fs.element()).toBe(document.documentElement);

    render(
      <Modal onClose={vi.fn()} title="A dialog">
        <p>Visible there.</p>
      </Modal>,
    );

    expect(fs.exit).not.toHaveBeenCalled();
  });

  it('shows toasts inside the fullscreen box, and back in the page after it', () => {
    const fs = stubFullscreen();
    render(
      <>
        <Speaker />
        {viewer}
      </>,
    );
    const viewport = () => screen.getByTestId('toast-viewport');
    expect(viewport().parentElement).toBe(document.body);

    fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }));
    const box = fs.element() as HTMLElement;
    expect(viewport().parentElement).toBe(box);

    fireEvent.click(screen.getByText('speak'));
    expect(within(box).getByText('Download failed')).toBeInTheDocument();
    // Live: the box is the topmost dialog's, and nothing above it is inert.
    expect(viewport().closest('[inert]')).toBeNull();

    fireEvent.click(within(box).getByRole('button', { name: 'Exit fullscreen' }));
    expect(viewport().parentElement).toBe(document.body);
    expect(within(viewport()).getByText('Download failed')).toBeInTheDocument();
  });

  it('keeps toasts in the page when the fullscreen element cannot hold them', () => {
    const fs = stubFullscreen();
    render(<Speaker />);
    const video = document.createElement('video');
    document.body.appendChild(video);
    try {
      act(() => {
        void video.requestFullscreen();
      });
      expect(fs.element()).toBe(video);
      expect(screen.getByTestId('toast-viewport').parentElement).toBe(document.body);
    } finally {
      video.remove();
    }
  });
});

describe('the image viewer in fullscreen', () => {
  const show = () => {
    const onClose = vi.fn();
    render(<ImagePreviewModal src="/api/v1/library/files/7/image" title="photo.png" fileSize={10} onClose={onClose} />);
    return { onClose };
  };

  it('has no fullscreen button on a browser without the API — absent, not broken', () => {
    show();

    expect(screen.getByRole('button', { name: 'Zoom in' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /fullscreen/i })).not.toBeInTheDocument();
  });

  it("puts the viewer's own element into fullscreen, with the picture and the way out inside it", () => {
    const fs = stubFullscreen();
    show();

    fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }));

    expect(fs.request).toHaveBeenCalledTimes(1);
    const element = fs.element() as HTMLElement;
    expect(element).not.toBeNull();
    expect(element).not.toBe(document.documentElement);
    expect(element).not.toBe(document.body);
    expect(within(element).getByRole('img', { name: 'photo.png' })).toBeInTheDocument();

    const leave = within(element).getByRole('button', { name: 'Exit fullscreen' });
    fireEvent.click(leave);
    expect(fs.exit).toHaveBeenCalledTimes(1);
    expect(fs.element()).toBeNull();
    expect(screen.getByRole('button', { name: 'Fullscreen' })).toBeInTheDocument();
  });

  it('gives Escape to the fullscreen first: it leaves fullscreen and the viewer stays open', () => {
    const fs = stubFullscreen();
    const { onClose } = show();
    fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }));

    pressEscape();

    expect(fs.exit).toHaveBeenCalledTimes(1);
    expect(fs.element()).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: 'photo.png' })).toBeInTheDocument();

    // The next Escape is an ordinary one: the modal stack closes the viewer.
    clock += ESC_GRACE_MS + 1;
    pressEscape();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does not close the viewer when the browser left fullscreen itself and delivers the key afterwards', () => {
    const fs = stubFullscreen();
    const { onClose } = show();
    fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }));

    fs.browserLeaves();
    clock += 30;
    pressEscape();

    expect(onClose).not.toHaveBeenCalled();
    expect(fs.exit).not.toHaveBeenCalled();

    clock += ESC_GRACE_MS + 1;
    pressEscape();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('leaves Escape alone when it was never fullscreen', () => {
    stubFullscreen();
    const { onClose } = show();

    pressEscape();

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('returns the focus to the toggle when leaving fullscreen dropped it', () => {
    const fs = stubFullscreen();
    show();
    const enter = screen.getByRole('button', { name: 'Fullscreen' });
    enter.focus();
    fireEvent.click(enter);

    // What a browser does to the focus when the fullscreen element changes.
    (document.activeElement as HTMLElement | null)?.blur();
    expect(document.activeElement).toBe(document.body);
    fs.browserLeaves();

    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Fullscreen' }));
  });
});

describe('the 3D / G-code viewer in fullscreen', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/archives/:id/capabilities', () =>
        HttpResponse.json({
          has_model: true,
          has_gcode: true,
          has_source: false,
          build_volume: { x: 256, y: 256, z: 256 },
          filament_colors: ['#00ae42'],
        }),
      ),
      http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
    );
  });

  const show = () => {
    const onClose = vi.fn();
    render(<ModelViewerModal archiveId={1} title="Test Model" onClose={onClose} />);
    return { onClose };
  };

  it('has no fullscreen button without the API, and keeps "fill the window"', async () => {
    show();

    await screen.findByTestId('model-viewer');
    expect(screen.getByTitle('Fill the window')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Fullscreen' })).not.toBeInTheDocument();
  });

  it('shows the viewer — its tabs, the model and the way out — in fullscreen, and nothing of the header', async () => {
    const fs = stubFullscreen();
    show();
    await screen.findByTestId('model-viewer');

    fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }));

    const element = fs.element() as HTMLElement;
    expect(element).not.toBeNull();
    expect(within(element).getByTestId('model-viewer')).toBeInTheDocument();
    expect(within(element).getByText('G-code Preview')).toBeInTheDocument();
    expect(within(element).getByRole('button', { name: 'Exit fullscreen' })).toBeInTheDocument();
    // The header opens dialogs (Slice…), and a dialog is not in this subtree.
    expect(within(element).queryByText('Test Model')).not.toBeInTheDocument();
    expect(within(element).queryByTitle('Fill the window')).not.toBeInTheDocument();
  });

  it('gives Escape to the fullscreen first, and "fill the window" is untouched by it', async () => {
    const fs = stubFullscreen();
    const { onClose } = show();
    await screen.findByTestId('model-viewer');

    fireEvent.click(screen.getByTitle('Fill the window'));
    expect(await screen.findByTitle('Restore size')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }));
    pressEscape();

    expect(fs.exit).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByTitle('Restore size')).toBeInTheDocument();

    clock += ESC_GRACE_MS + 1;
    pressEscape();
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });
});
