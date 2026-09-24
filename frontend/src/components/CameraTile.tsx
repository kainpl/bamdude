import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Expand, VideoOff, WifiOff } from 'lucide-react';
import { getAuthToken, withStreamToken } from '../api/client';
import { formatDuration } from '../utils/date';
import { useCameraImageRef } from '../hooks/useCameraImageRef';
import { useConnection } from '../contexts/ConnectionContext';
import { sourceKey, stopPath, streamPath, type CameraSource } from '../utils/cameraSource';
import { CameraSnapshotImage } from './CameraSnapshotImage';

export type CameraTileMode = 'live' | 'snapshot' | 'paused';
export type CameraTileStatusMode = 'off' | 'compact' | 'full';

interface CameraTileProps {
  /** A printer's camera, or a camera that belongs to a place. */
  source: CameraSource;
  name: string;
  cameraRotation?: number;
  mode: CameraTileMode;
  snapshotIntervalMs: number;
  connected: boolean;
  /** The one explicit live-view selection on an interactive wall. */
  onToggleLive?: () => void;
  /** Opens the shared M-size printer card without changing the live selection. */
  onOpenPrinterCard?: () => void;
  activeLive?: boolean;
  // Optional status overlay — wired by CameraWall from the shared
  // ['printerStatus', id] query. All optional so existing tests don't break.
  statusMode?: CameraTileStatusMode;
  printerState?: string | null;
  progress?: number | null;
  remainingMin?: number | null;
  layerNum?: number | null;
  totalLayers?: number | null;
  printName?: string | null;
  /** Kiosk mode (upstream #2531): authenticate the stream with this long-lived
   *  token instead of the module-cached short-lived one, which only a logged-in
   *  browser ever holds. */
  streamToken?: string;
  hmsErrorCount?: number;
}

// Tiles render lighter than EmbeddedCameraViewer's full window: lower fps,
// no drag/resize/zoom shell, and snapshot fallback when off-cap. The server
// still does the MJPEG fan-out, so per-tile cost is one TLS pull on the wire.
const LIVE_FPS = 8;


type StatusBucket = 'printing' | 'paused' | 'finished' | 'error' | 'idle';

function classifyState(state: string | null | undefined, hmsErrorCount: number): StatusBucket {
  if (hmsErrorCount > 0) return 'error';
  switch (state) {
    case 'RUNNING':
      return 'printing';
    case 'PAUSE':
      return 'paused';
    case 'FAILED':
      return 'error';
    case 'FINISH':
      return 'finished';
    default:
      return 'idle';
  }
}

const BUCKET_CHIP_CLASS: Record<StatusBucket, string> = {
  printing: 'bg-bambu-green/85 text-white',
  paused: 'bg-amber-500/85 text-black',
  finished: 'bg-sky-500/80 text-white',
  error: 'bg-red-500/85 text-white',
  idle: 'bg-bambu-dark-tertiary/80 text-bambu-gray',
};

export function CameraTile({
  source,
  name,
  cameraRotation = 0,
  mode,
  snapshotIntervalMs,
  connected,
  onToggleLive,
  onOpenPrinterCard,
  activeLive = false,
  statusMode = 'off',
  printerState = null,
  progress = null,
  remainingMin = null,
  layerNum = null,
  totalLayers = null,
  printName = null,
  hmsErrorCount = 0,
  streamToken,
}: CameraTileProps) {
  const { t } = useTranslation();
  const [bust, setBust] = useState(0);
  const [errored, setErrored] = useState(false);
  const lastModeRef = useRef<CameraTileMode>(mode);
  const { isConnected: serverConnected, showOfflineIndicator } = useConnection();
  const wasOfflineRef = useRef(showOfflineIndicator);

  useEffect(() => {
    // A browser can keep the last decoded MJPEG frame after the server closes
    // the stream, without firing img.onerror. Restart that image when the
    // shared server connection returns instead of showing a stale frame.
    if (wasOfflineRef.current && !showOfflineIndicator && serverConnected && mode === 'live') {
      setErrored(false);
      setBust((b) => b + 1);
    }
    wasOfflineRef.current = showOfflineIndicator;
  }, [mode, serverConnected, showOfflineIndicator]);

  // Tell the backend to release its MJPEG transcoder when this tile stops
  // being live — either by unmounting or by transitioning to snapshot/paused.
  // EmbeddedCameraViewer uses the same /camera/stop with keepalive on unmount.
  useEffect(() => {
    const wasLive = lastModeRef.current === 'live';
    const isLive = mode === 'live';
    lastModeRef.current = mode;
    if (wasLive && !isLive) {
      const headers: Record<string, string> = {};
      const token = getAuthToken();
      if (token) headers['Authorization'] = `Bearer ${token}`;
      fetch(stopPath(source), {
        method: 'POST',
        keepalive: true,
        headers,
      }).catch(() => {});
    }
    setErrored(false);
    setBust((b) => b + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, sourceKey(source)]);

  useEffect(() => {
    return () => {
      if (lastModeRef.current === 'live') {
        const headers: Record<string, string> = {};
        const token = getAuthToken();
        if (token) headers['Authorization'] = `Bearer ${token}`;
        fetch(stopPath(source), {
          method: 'POST',
          keepalive: true,
          headers,
        }).catch(() => {});
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceKey(source)]);

  // A kiosk carries its own token; everything else rides the module-cached
  // short-lived one that only a signed-in browser holds (upstream #2531).
  const withToken = (path: string) =>
    streamToken ? `${path}&token=${encodeURIComponent(streamToken)}` : withStreamToken(path);
  const liveUrl = withToken(streamPath(source, LIVE_FPS, bust));
  const { attachImage: attachLiveImage } = useCameraImageRef(liveUrl);

  const transform = cameraRotation ? `rotate(${cameraRotation}deg)` : undefined;

  const bucket = classifyState(printerState, hmsErrorCount);
  // Hide chip for idle to keep cold walls clean; always show when something
  // is happening (printing/paused/finished/error).
  // A wall may hide routine status chips, but an error or pause must still
  // tell an operator where to look before they choose a live camera.
  const showChip = connected && bucket !== 'idle' && (
    statusMode !== 'off' || bucket === 'error' || bucket === 'paused'
  );
  const isPrintingOrPaused = bucket === 'printing' || bucket === 'paused';
  const showInfoStrip = connected && statusMode === 'full' && isPrintingOrPaused;
  const fileLabel = printName ?? null;
  const progressPct = progress != null ? Math.round(progress) : null;
  const hasLayers = layerNum != null && totalLayers != null && totalLayers > 0;
  const hasRemaining = remainingMin != null && remainingMin > 0;

  // Kiosk walls pass neither handler. They remain passive and redacted, while
  // an authenticated operator gets two distinct actions: select live, or open
  // the printer card. These cannot be nested HTML buttons.
  const interactive = onToggleLive != null;
  const attentionClass = !connected
    ? ' border-bambu-dark-tertiary'
    : bucket === 'error'
      ? ' border-red-500 ring-1 ring-red-500/70'
      : bucket === 'paused'
        ? ' border-amber-400 ring-1 ring-amber-400/60'
        : activeLive
          ? ' border-bambu-green ring-1 ring-bambu-green/75'
          : ' border-bambu-dark-tertiary';
  const rootClass =
    'group relative aspect-video w-full overflow-hidden rounded-lg border bg-black text-left' + attentionClass +
    (interactive ? '' : ' cursor-default');

  return (
    <div
      className={rootClass}
      title={name}
    >
      {interactive && (
        <button
          type="button"
          onClick={onToggleLive}
          aria-label={t(activeLive ? 'printers.camWall.stopLive' : 'printers.camWall.startLive', { printer: name })}
          className="absolute inset-0 z-10 cursor-pointer rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green focus-visible:ring-inset"
        />
      )}
      {!connected || mode === 'paused' ? (
        <div className="absolute inset-0 flex items-center justify-center bg-bambu-dark/60">
          {connected ? (
            <VideoOff className="h-8 w-8 text-bambu-gray/70" aria-hidden="true" />
          ) : (
            <WifiOff className="h-8 w-8 text-bambu-gray/70" aria-hidden="true" />
          )}
        </div>
      ) : mode === 'snapshot' ? (
        <CameraSnapshotImage
          source={source}
          name={name}
          intervalMs={snapshotIntervalMs}
          streamToken={streamToken}
          transform={transform}
        />
      ) : errored ? (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-black/80 text-bambu-gray">
          <VideoOff className="h-7 w-7" aria-hidden="true" />
          <span className="text-xs">{t('printers.camWall.noSignal')}</span>
        </div>
      ) : (
        <img
          ref={attachLiveImage}
          key={`${mode}-${bust}`}
          src={liveUrl}
          alt={name}
          draggable={false}
          loading="lazy"
          className="h-full w-full select-none object-contain"
          style={{ transform }}
          onError={() => setErrored(true)}
        />
      )}

      {mode === 'live' && showOfflineIndicator && (
        <div role="status" className="pointer-events-none absolute inset-x-2 top-9 z-20 flex items-center justify-center gap-1.5 rounded bg-black/80 px-2 py-1 text-xs text-amber-300">
          <WifiOff className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>{t('common.reconnecting')}</span>
        </div>
      )}

      {/* Status chip (top-left) */}
      {showChip && (
        <span
          className={`pointer-events-none absolute left-2 top-2 z-20 flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${BUCKET_CHIP_CLASS[bucket]}`}
        >
          {hmsErrorCount > 0 && (
            <AlertTriangle
              className="h-3 w-3"
              aria-hidden="true"
            />
          )}
          <span>{t(`printers.status.${bucket}`)}</span>
        </span>
      )}

      {/* Mode indicator (top-right) */}
      <span
        className={`pointer-events-none absolute right-2 top-2 z-20 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
          mode === 'live'
            ? 'bg-red-500/80 text-white'
            : mode === 'snapshot'
              ? 'bg-amber-500/70 text-black'
              : 'bg-bambu-dark-tertiary/70 text-bambu-gray'
        }`}
      >
        {mode === 'live'
          ? t('printers.camWall.live')
          : mode === 'snapshot'
            ? t('printers.camWall.snap')
            : t('printers.camWall.off')}
      </span>

      {/* Bottom overlay: name + (when full) print info */}
      <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 via-black/55 to-transparent px-2 pb-1.5 pt-3 text-white">
        {showInfoStrip && (
          <div className="mb-0.5 space-y-0.5 text-[11px] leading-tight text-white/90">
            {fileLabel && (
              <div className="truncate" title={fileLabel}>
                {fileLabel}
              </div>
            )}
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-bambu-gray">
              {progressPct != null && (
                <span className="font-semibold text-white">{progressPct}%</span>
              )}
              {hasLayers && (
                <span>
                  {t('printers.camWall.layer', {
                    cur: layerNum,
                    total: totalLayers,
                  })}
                </span>
              )}
              {hasRemaining && (
                <span>
                  {t('printers.camWall.timeLeft', {
                    time: formatDuration((remainingMin ?? 0) * 60),
                  })}
                </span>
              )}
            </div>
          </div>
        )}
        <span className={`block truncate text-xs font-medium${connected && mode === 'snapshot' ? ' pr-36' : ''}`}>{name}</span>
      </div>
      {onOpenPrinterCard && (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            onOpenPrinterCard();
          }}
          aria-label={t('printers.camWall.openPrinterCard', { printer: name })}
          title={t('printers.camWall.openPrinterCard')}
          className="absolute bottom-2 right-2 z-20 inline-flex h-7 w-7 items-center justify-center rounded-md bg-black/70 text-white transition-colors hover:bg-bambu-dark-tertiary focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
        >
          <Expand className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
    </div>
  );
}
