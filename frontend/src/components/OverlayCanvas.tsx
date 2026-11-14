/**
 * The stream overlay drawn at a FIXED size and scaled to the browser source
 * (`?layout=landscape|portrait`).
 *
 * Without it the overlay is sized by the window it happens to be in, so the
 * same address looks different in the builder's preview, in a browser tab and
 * in OBS. With it the composition is laid out once, at the resolution the
 * source is meant to have, and then scaled as a whole: text, picture and
 * spacing keep their proportions wherever it is shown, and a source of another
 * shape gets bars instead of a rearranged overlay.
 */
import { useCallback, useState, type CSSProperties, type ReactNode } from 'react';
import { OVERLAY_CANVAS, type OverlayLayout } from '../utils/overlayOptions';

interface OverlayCanvasProps {
  layout: OverlayLayout;
  /** What shows around the canvas in a source of another shape. */
  ground: string;
  children: ReactNode;
}

export function OverlayCanvas({ layout, ground, children }: OverlayCanvasProps) {
  const { width, height } = OVERLAY_CANVAS[layout];
  const [scale, setScale] = useState(0);

  // A ref callback measures in the commit, before paint: the first frame is
  // already at its scale.
  const hostRef = useCallback((host: HTMLDivElement | null) => {
    if (!host) return;
    const measure = () => setScale(Math.min(host.clientWidth / width, host.clientHeight / height));
    measure();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure);
      return () => window.removeEventListener('resize', measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(host);
    return () => observer.disconnect();
  }, [width, height]);

  const canvasStyle: CSSProperties = {
    position: 'absolute',
    top: '50%',
    left: '50%',
    width,
    height,
    transform: `translate(-50%, -50%) scale(${scale})`,
    transformOrigin: 'center',
  };

  return (
    <div
      ref={hostRef}
      data-overlay-canvas={layout}
      className="relative h-screen w-full overflow-hidden"
      style={{ background: ground }}
    >
      <div style={canvasStyle}>{children}</div>
    </div>
  );
}
