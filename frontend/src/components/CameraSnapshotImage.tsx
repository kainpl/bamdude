import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { withStreamToken } from '../api/client';
import { acquireCameraSnapshotSlot } from '../utils/cameraSnapshotQueue';
import { snapshotPath, sourceKey, type CameraSource } from '../utils/cameraSource';

interface CameraSnapshotImageProps {
  /** A printer's camera or a camera of its own — only the URL differs. */
  source: CameraSource;
  name: string;
  intervalMs: number;
  streamToken?: string;
  transform?: string;
}

export function CameraSnapshotImage({
  source, name, intervalMs, streamToken, transform,
}: CameraSnapshotImageProps) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const imageRef = useRef<HTMLImageElement>(null);
  const [lastFrameAt, setLastFrameAt] = useState<number | null>(null);
  const hasFrame = lastFrameAt !== null;
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const lifetime = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let displayedUrl: string | undefined;
    // Capture this element: cleanup must not clear a replacement tile's image.
    const image = imageRef.current;
    setLastFrameAt(null);
    setFailed(false);

    const refresh = async () => {
      let release: (() => void) | undefined;
      let deadline: ReturnType<typeof setTimeout> | undefined;
      let candidateUrl: string | undefined;
      const request = new AbortController();
      const cancelRequest = () => request.abort();
      lifetime.signal.addEventListener('abort', cancelRequest, { once: true });
      try {
        release = await acquireCameraSnapshotSlot(lifetime.signal);
        lifetime.signal.throwIfAborted();
        // The deadline covers headers AND the JPEG body. Start it after the
        // slot grant, so waiting tiles cannot time out before their first turn.
        deadline = setTimeout(cancelRequest, 20_000);
        // `poll` declares this tile's cadence: the server then keeps the
        // chamber light (when the farm asks for it) for that long after each
        // frame instead of blinking it on every one. Only a printer has one.
        const path = snapshotPath(source, { bust: Date.now(), pollMs: Math.max(1000, intervalMs) });
        const url = streamToken ? `${path}&token=${encodeURIComponent(streamToken)}` : withStreamToken(path);
        const response = await fetch(url, { signal: request.signal, cache: 'no-store' });
        if (!response.ok) {
          if (response.status === 401 && !streamToken) {
            void queryClient.invalidateQueries({ queryKey: ['camera-stream-token'] });
          }
          await response.body?.cancel();
          throw new Error(`Snapshot HTTP ${response.status}`);
        }
        const blob = await response.blob();
        request.signal.throwIfAborted();
        candidateUrl = URL.createObjectURL(blob);
        const nextImage = new Image();
        nextImage.src = candidateUrl;
        // Decode off-screen before swapping the visible image. A pending or
        // broken response must never erase the last successfully shown frame.
        await new Promise<void>((resolve, reject) => {
          const aborted = () => reject(request.signal.reason);
          request.signal.addEventListener('abort', aborted, { once: true });
          nextImage.decode().then(resolve, reject).finally(() => {
            request.signal.removeEventListener('abort', aborted);
          });
        });
        request.signal.throwIfAborted();
        if (image) image.src = candidateUrl;
        if (displayedUrl) URL.revokeObjectURL(displayedUrl);
        displayedUrl = candidateUrl;
        candidateUrl = undefined;
        setLastFrameAt(Date.now());
        setFailed(false);
      } catch {
        if (!lifetime.signal.aborted) setFailed(true);
      } finally {
        clearTimeout(deadline);
        if (candidateUrl) URL.revokeObjectURL(candidateUrl);
        lifetime.signal.removeEventListener('abort', cancelRequest);
        release?.();
        // One request per tile; the interval starts only after it settles.
        if (!lifetime.signal.aborted) timer = setTimeout(() => void refresh(), Math.max(1000, intervalMs));
      }
    };
    void refresh();
    return () => {
      lifetime.abort();
      clearTimeout(timer);
      image?.removeAttribute('src');
      if (displayedUrl) URL.revokeObjectURL(displayedUrl);
    };
    // `sourceKey` rather than the object: a fresh `{kind, id}` literal on every
    // render would restart the poll loop each time the parent re-renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceKey(source), intervalMs, streamToken, queryClient]);

  return <>
    <img
      ref={imageRef}
      alt={name}
      draggable={false}
      className="h-full w-full select-none object-contain"
      style={{ transform, visibility: hasFrame ? 'visible' : 'hidden' }}
    />
    {lastFrameAt !== null && <time
      dateTime={new Date(lastFrameAt).toISOString()}
      title={t('printers.camWall.frameReceivedAt', { time: new Date(lastFrameAt).toLocaleString(i18n.language) })}
      className="absolute bottom-1.5 right-2 z-10 rounded bg-black/80 px-1.5 py-0.5 text-[10px] tabular-nums text-white/90"
    >
      {t('printers.camWall.frameUpdatedAt', { time: new Date(lastFrameAt).toLocaleTimeString(i18n.language, { hour12: false }) })}
    </time>}
    {failed && <span className="absolute inset-x-2 top-9 rounded bg-black/80 px-2 py-1 text-center text-xs text-bambu-gray" role="status">
      {t(hasFrame ? 'printers.camWall.snapshotStale' : 'printers.camWall.noSignal')}
    </span>}
  </>;
}
