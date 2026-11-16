/**
 * An image file, opened: the picture fitted to the window, zoom and pan.
 *
 * The picture is an `<img>` (never an embedded document — an `<img>` runs
 * nothing), so its URL must carry its own credential: the caller builds it with
 * the media-token URL builder.
 *
 * Geometry is one inline transform, `translate(x, y) scale(s)` about the
 * picture's centre: `s = 1` is "fitted to the stage", and the picture never
 * goes below it. Zooming keeps the point under the pointer where it is; a
 * zoomed picture pans only until its edge meets the stage's.
 *
 * ⚠️ What a tab can decode is bounded twice: a file over `MAX_IMAGE_BYTES` is
 * never requested, and a decoded picture over `MAX_IMAGE_PIXELS` is dropped
 * again — a 20 000 × 20 000 PNG is a few megabytes on disk and 1.6 GB as a
 * bitmap.
 */
import { useCallback, useEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { ImageOff, ZoomIn, ZoomOut } from 'lucide-react';
import { FilePreviewShell, PREVIEW_CONTROL_CLASS } from './FilePreviewShell';

export const MAX_IMAGE_BYTES = 100 * 1024 * 1024;
export const MAX_IMAGE_PIXELS = 100_000_000;

const STEP = 1.25;
const MIN_SCALE = 1;
const MAX_SCALE = 16;
/** Pointer travel, in px, past which a press is a drag and not a click. */
const DRAG_SLOP = 3;

interface View {
  scale: number;
  x: number;
  y: number;
}
const FITTED: View = { scale: 1, x: 0, y: 0 };

interface Props {
  /** The picture's URL, token included. */
  src: string;
  title: string;
  /** The file's size in bytes, when known. */
  fileSize?: number | null;
  onClose: () => void;
}

export function ImagePreviewModal({ src, title, fileSize, onClose }: Props) {
  const { t } = useTranslation();
  const tooManyBytes = fileSize != null && fileSize > MAX_IMAGE_BYTES;
  const [status, setStatus] = useState<'loading' | 'ready' | 'failed' | 'too_large'>(tooManyBytes ? 'too_large' : 'loading');
  const [view, setView] = useState<View>(FITTED);
  const stageRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const dragged = useRef(false);

  /** A view the stage allows: the scale in range, the picture's edges never inside the stage's. */
  const settle = useCallback((next: View): View => {
    const scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next.scale));
    if (scale === MIN_SCALE) return FITTED;
    const stage = stageRef.current;
    const img = imgRef.current;
    const limitX = stage && img ? Math.max(0, (img.offsetWidth * scale - stage.clientWidth) / 2) : 0;
    const limitY = stage && img ? Math.max(0, (img.offsetHeight * scale - stage.clientHeight) / 2) : 0;
    return {
      scale,
      x: Math.min(limitX, Math.max(-limitX, next.x)),
      y: Math.min(limitY, Math.max(-limitY, next.y)),
    };
  }, []);

  /** Zoom by `factor`, keeping the stage point `anchor` (from the stage's centre) where it is. */
  const zoomBy = useCallback(
    (factor: number, anchor: { x: number; y: number } = { x: 0, y: 0 }) => {
      setView((v) => {
        const scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, v.scale * factor));
        const k = scale / v.scale;
        return settle({ scale, x: anchor.x - (anchor.x - v.x) * k, y: anchor.y - (anchor.y - v.y) * k });
      });
    },
    [settle],
  );
  const reset = useCallback(() => setView(FITTED), []);

  /** A client point, measured from the stage's centre. */
  const fromCentre = useCallback((clientX: number, clientY: number) => {
    const rect = stageRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return { x: clientX - (rect.left + rect.width / 2), y: clientY - (rect.top + rect.height / 2) };
  }, []);

  const ready = status === 'ready' || status === 'loading';

  // The wheel. Native and non-passive: React's own wheel listener is passive,
  // and without preventDefault the page behind would take the scroll.
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage || !ready) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      zoomBy(Math.exp(-e.deltaY * 0.0015), fromCentre(e.clientX, e.clientY));
    };
    stage.addEventListener('wheel', onWheel, { passive: false });
    return () => stage.removeEventListener('wheel', onWheel);
  }, [ready, zoomBy, fromCentre]);

  // + / − / 0. Escape is the shell's (the modal stack). Not while another
  // dialog sits above this one, and never with a modifier — Ctrl + is the
  // browser's own zoom.
  useEffect(() => {
    if (!ready) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (stageRef.current?.closest('[inert]')) return;
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return;
      if (e.key === '+' || e.key === '=') zoomBy(STEP);
      else if (e.key === '-' || e.key === '_') zoomBy(1 / STEP);
      else if (e.key === '0') reset();
      else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [ready, zoomBy, reset]);

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (pointers.current.size === 0) dragged.current = false;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    // Keeps the drag when the pointer leaves the stage (absent in some test DOMs).
    try {
      e.currentTarget.setPointerCapture?.(e.pointerId);
    } catch {
      /* a pointer that is already gone */
    }
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const before = pointers.current.get(e.pointerId);
    if (!before) return;
    const now = { x: e.clientX, y: e.clientY };
    if (pointers.current.size === 1) {
      const dx = now.x - before.x;
      const dy = now.y - before.y;
      if (!dragged.current && Math.hypot(dx, dy) < DRAG_SLOP) return;
      dragged.current = true;
      pointers.current.set(e.pointerId, now);
      setView((v) => (v.scale === MIN_SCALE ? v : settle({ scale: v.scale, x: v.x + dx, y: v.y + dy })));
      return;
    }
    // Two fingers: the change of the distance between them is the zoom.
    const other = [...pointers.current.entries()].find(([id]) => id !== e.pointerId)?.[1];
    pointers.current.set(e.pointerId, now);
    if (!other) return;
    const was = Math.hypot(before.x - other.x, before.y - other.y);
    const is = Math.hypot(now.x - other.x, now.y - other.y);
    if (was <= 0 || is <= 0) return;
    dragged.current = true;
    zoomBy(is / was, fromCentre((now.x + other.x) / 2, (now.y + other.y) / 2));
  };

  const onPointerEnd = (e: ReactPointerEvent<HTMLDivElement>) => {
    pointers.current.delete(e.pointerId);
  };

  // The stage's empty part is the lightbox's ground: a click there closes —
  // unless it is the click that ends a drag.
  const onStageClick = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (dragged.current) {
      dragged.current = false;
      return;
    }
    if (e.target === e.currentTarget) onClose();
  };

  const onLoad = () => {
    const img = imgRef.current;
    if (img && img.naturalWidth * img.naturalHeight > MAX_IMAGE_PIXELS) setStatus('too_large');
    else setStatus('ready');
  };

  const controls = ready ? (
    <>
      <button
        type="button"
        className={PREVIEW_CONTROL_CLASS}
        onClick={() => zoomBy(1 / STEP)}
        disabled={view.scale <= MIN_SCALE}
        aria-label={t('filePreview.zoomOut')}
        title={t('filePreview.zoomOut')}
      >
        <ZoomOut className="h-5 w-5" />
      </button>
      <button
        type="button"
        className={`${PREVIEW_CONTROL_CLASS} min-w-14 tabular-nums`}
        onClick={reset}
        aria-label={t('filePreview.resetZoom')}
        title={t('filePreview.resetZoom')}
      >
        {Math.round(view.scale * 100)}%
      </button>
      <button
        type="button"
        className={PREVIEW_CONTROL_CLASS}
        onClick={() => zoomBy(STEP)}
        disabled={view.scale >= MAX_SCALE}
        aria-label={t('filePreview.zoomIn')}
        title={t('filePreview.zoomIn')}
      >
        <ZoomIn className="h-5 w-5" />
      </button>
    </>
  ) : null;

  return (
    <FilePreviewShell title={title} onClose={onClose} controls={controls}>
      {ready ? (
        <div
          ref={stageRef}
          data-testid="image-stage"
          className={`absolute inset-0 flex touch-none select-none items-center justify-center p-4 ${
            view.scale > MIN_SCALE ? 'cursor-grab active:cursor-grabbing' : ''
          }`}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerEnd}
          onPointerCancel={onPointerEnd}
          onClick={onStageClick}
        >
          <img
            ref={imgRef}
            src={src}
            alt={title}
            draggable={false}
            onLoad={onLoad}
            onError={() => setStatus('failed')}
            className="max-h-full max-w-full object-contain"
            style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})` }}
          />
        </div>
      ) : (
        <div
          className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6 text-center text-bambu-gray"
          onClick={(e) => {
            if (e.target === e.currentTarget) onClose();
          }}
        >
          <ImageOff className="h-10 w-10" />
          <p>{status === 'too_large' ? t('filePreview.imageTooLarge') : t('filePreview.imageFailed')}</p>
        </div>
      )}
    </FilePreviewShell>
  );
}
