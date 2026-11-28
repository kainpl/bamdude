import { useEffect, useRef, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Box, Expand, Flame, RectangleHorizontal, VideoOff, WifiOff } from 'lucide-react';
import { api, getAuthToken, withStreamToken } from '../api/client';
import { formatDuration, formatETA, type TimeFormat } from '../utils/date';
import type { CamWallControlsMode } from './camWallControls';
import { useCameraImageRef } from '../hooks/useCameraImageRef';
import { useCameraStreamRecovery } from '../hooks/useCameraStreamRecovery';
import { fillStyle, useRotatedBox } from '../hooks/useRotatedFit';
import { useConnection } from '../contexts/ConnectionContext';
import { sourceKey, stopPath, streamPath, type CameraSource } from '../utils/cameraSource';
import { CameraSnapshotImage } from './CameraSnapshotImage';

export type CameraTileMode = 'live' | 'snapshot' | 'paused';
export type CameraTileStatusMode = 'off' | 'compact' | 'full';

/** The heater readings a tile shows in the full overlay (upstream #3479). */
export interface CameraTileTemperatures {
  nozzle?: number;
  nozzle_2?: number;
  bed?: number;
  chamber?: number;
}

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
  // Full overlay only (upstream #3479): the heaters, on every connected tile, and
  // the finish time beside the time left while printing. The kiosk feed carries
  // no temperatures, so a kiosk tile shows none.
  temperatures?: CameraTileTemperatures | null;
  timeFormat?: TimeFormat;
  /** Pause/resume, stop and the chamber light (``CameraTileControls``), drawn above
   *  the tile-wide live toggle like the tile's other buttons. Given only on a wall
   *  whose user may control printers; never on a kiosk wall. */
  controls?: ReactNode;
  /** Whether ``controls`` wait for hover (and focus) or are always on screen. A
   *  touch screen has no hover and always shows them. */
  controlsMode?: CamWallControlsMode;
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
  temperatures = null,
  timeFormat = 'system',
  controls,
  controlsMode = 'hover',
}: CameraTileProps) {
  const { t } = useTranslation();
  const lastModeRef = useRef<CameraTileMode>(mode);
  const { isConnected: serverConnected, showOfflineIndicator } = useConnection();
  const wasOfflineRef = useRef(showOfflineIndicator);

  // A stream the server ends cleanly fires nothing on the image: the tile asks
  // the server about its source, and re-opens with a bounded backoff. A kiosk
  // wall's token may not read that status (and a kiosk wall has no live tile);
  // there only the image's own `error` starts a retry.
  const {
    phase: livePhase,
    generation,
    viewerId,
    onError: onLiveError,
    onLoad: onLiveLoad,
    restart: restartLive,
  } = useCameraStreamRecovery({
    enabled: mode === 'live' && connected,
    readHealth: streamToken ? undefined : (viewer) => api.getCameraSourceStatus(source, viewer),
  });

  useEffect(() => {
    // A browser can keep the last decoded MJPEG frame after the server closes
    // the stream, without firing img.onerror. Restart that image when the
    // shared server connection returns instead of showing a stale frame.
    if (wasOfflineRef.current && !showOfflineIndicator && serverConnected && mode === 'live') {
      restartLive();
    }
    wasOfflineRef.current = showOfflineIndicator;
  }, [mode, serverConnected, showOfflineIndicator, restartLive]);

  // A new selection starts on a fresh image and a fresh address — and opens
  // ONE stream. The generation is therefore moved only while no live image is
  // on the page: when the tile LEAVES live, or when its recovery is waiting.
  // Moving it in the commit that has just mounted the live image (snapshot →
  // live by props, as the wall does) re-keyed that image: connect, abort,
  // connect against a printer whose firmware allows one camera socket.
  const selection = `${mode}|${sourceKey(source)}`;
  const lastSelectionRef = useRef({ selection, mode });
  useEffect(() => {
    const last = lastSelectionRef.current;
    if (last.selection === selection) return;
    lastSelectionRef.current = { selection, mode };
    // Live before and live now is another source: its address is new by its
    // path, and the mounted image is simply given it.
    if (mode === 'live' && (last.mode !== 'live' || livePhase === 'live')) return;
    restartLive();
  }, [selection, mode, livePhase, restartLive]);

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
  const liveUrl = withToken(streamPath(source, LIVE_FPS, generation, viewerId));
  const { attachImage: attachLiveImage } = useCameraImageRef(liveUrl);

  // A sideways camera is laid out in the tile's sides swapped and centred, or
  // the turned picture is cut off at the tile's top and bottom. One measure
  // serves the live image and the snapshot.
  const { boxRef: tileRef, box: tileBox } = useRotatedBox(cameraRotation);
  const pictureStyle = fillStyle(tileBox, cameraRotation);

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
  // Heaters a printer has no sensor for are absent from its status, not 0.
  const showTemps = connected && statusMode === 'full' && temperatures != null;
  const nozzleTemps = [temperatures?.nozzle, temperatures?.nozzle_2].filter((v): v is number => v != null);
  const hasTemps = showTemps && (nozzleTemps.length > 0 || temperatures?.bed != null || temperatures?.chamber != null);

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
      ref={tileRef}
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
          style={pictureStyle}
        />
      ) : livePhase === 'failed' ? (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-black/80 text-bambu-gray">
          <VideoOff className="h-7 w-7" aria-hidden="true" />
          <span className="text-xs">{t('printers.camWall.noSignal')}</span>
          {/* Above the tile-wide live toggle, or the click would deselect the tile. */}
          <button
            type="button"
            onClick={restartLive}
            className="relative z-20 mt-1 rounded bg-bambu-dark-tertiary px-2 py-0.5 text-xs text-white hover:bg-bambu-green focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
          >
            {t('common.retry')}
          </button>
        </div>
      ) : livePhase === 'waiting' ? (
        // The dead image is gone (its request aborted) for the whole wait:
        // there is never a second stream beside a live one.
        <div role="status" className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-black/80 text-bambu-gray">
          <VideoOff className="h-7 w-7" aria-hidden="true" />
          <span className="text-xs">{t('common.reconnecting')}</span>
        </div>
      ) : (
        // No `src` prop: the hook gives the mounted image its URL.
        <img
          ref={attachLiveImage}
          key={`${mode}-${generation}`}
          alt={name}
          draggable={false}
          loading="lazy"
          className="h-full w-full select-none object-contain"
          style={pictureStyle}
          onError={onLiveError}
          onLoad={onLiveLoad}
        />
      )}

      {mode === 'live' && showOfflineIndicator && livePhase === 'live' && (
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
              {/* A paused print has no finish time: it moves with the pause. */}
              {hasRemaining && bucket === 'printing' && (
                <span>{t('printers.camWall.eta', { time: formatETA(remainingMin ?? 0, timeFormat, t) })}</span>
              )}
            </div>
          </div>
        )}
        {hasTemps && (
          <div className="mb-0.5 flex flex-wrap items-center gap-x-2 text-[11px] leading-tight text-bambu-gray">
            {nozzleTemps.length > 0 && (
              <span className="flex items-center gap-0.5" title={t('printers.temperatures.nozzle')}>
                <Flame className="h-3 w-3" aria-label={t('printers.temperatures.nozzle')} />
                {nozzleTemps.map((v) => `${Math.round(v)}°`).join(' / ')}
              </span>
            )}
            {temperatures?.bed != null && (
              <span className="flex items-center gap-0.5" title={t('printers.temperatures.bed')}>
                <RectangleHorizontal className="h-3 w-3" aria-label={t('printers.temperatures.bed')} />
                {Math.round(temperatures.bed)}°
              </span>
            )}
            {temperatures?.chamber != null && (
              <span className="flex items-center gap-0.5" title={t('printers.temperatures.chamber')}>
                <Box className="h-3 w-3" aria-label={t('printers.temperatures.chamber')} />
                {Math.round(temperatures.chamber)}°
              </span>
            )}
          </div>
        )}
        <span className={`block truncate text-xs font-medium${connected && mode === 'snapshot' ? ' pr-36' : ''}`}>{name}</span>
      </div>
      {controls && (
        // Above the tile-wide live toggle, like the tile's other buttons. Shown on
        // hover or focus where there is a pointer, always on a touch screen.
        <div
          data-testid="camwall-tile-controls"
          className={`absolute right-2 top-8 z-20 ${
            controlsMode === 'always'
              ? ''
              : 'opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100'
          }`}
        >
          {controls}
        </div>
      )}
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
