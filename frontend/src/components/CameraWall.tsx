import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cameraWallPrintName } from '../utils/cameraWall';
import { useQueries } from '@tanstack/react-query';
import { Settings as SettingsIcon } from 'lucide-react';
import { useCameraLiveBudget } from '../hooks/useCameraLiveBudget';
import { CameraTile, type CameraTileMode, type CameraTileStatusMode } from './CameraTile';
import { printerSource, sourceKey, standaloneSource, type CameraSource } from '../utils/cameraSource';
import { filterKnownHMSErrors } from './HMSErrorModal';
import { api, type HMSError, type PrinterStatus } from '../api/client';

// The wall only ever reads these fields off a printer, so it asks for no more
// than that. `Printer[]` satisfies this structurally, and so does the smaller
// payload the token-authenticated kiosk feed returns (upstream #2531) — which
// deliberately carries neither serial number nor IP address.
export interface CameraWallPrinter {
  id: number;
  name: string;
  camera_rotation?: number;
}

/** A camera that belongs to no printer. The kiosk feed serves exactly this
 *  shape and, deliberately, no URL — an RTSP camera's credentials live in one. */
export interface CameraWallCamera {
  id: number;
  name: string;
  rotation?: number;
}

/** One grid cell, whichever kind of source is behind it. */
interface WallTile {
  key: string;
  source: CameraSource;
  name: string;
  rotation?: number;
  /** Present only for a printer — the status overlay and the card need it. */
  printer?: CameraWallPrinter;
}

// What a tile draws from a printer's status. `PrinterStatus` satisfies it; so
// does the kiosk feed's entry, which is how the kiosk page feeds the same
// component without the JWT-gated per-printer status endpoint.
export interface CameraWallStatus {
  connected?: boolean;
  state?: string | null;
  progress?: number | null;
  remaining_time?: number | null;
  layer_num?: number | null;
  total_layers?: number | null;
  subtask_name?: string | null;
  current_print?: string | null;
  gcode_file?: string | null;
  // Codes only — enough to run the same filterKnownHMSErrors() on both the
  // authenticated wall and the kiosk feed, so the error chip means the same
  // thing in either mode.
  hms_errors?: HMSError[];
}

interface CameraWallProps {
  printers: CameraWallPrinter[];
  /** Cameras that belong to no printer — a room, a shelf, a dryer. Drawn
   *  after the printers, by the same tile: a view has no print to report. */
  cameras?: CameraWallCamera[];
  snapshotIntervalSec: number;
  statusMode: CameraTileStatusMode;
  /** Omitted by the kiosk wall: its token cannot open the detailed card. */
  onOpenPrinterCard?: (printerId: number, printerName: string) => void;
  onChangeSnapshotIntervalSec: (next: number) => void;
  onChangeStatusMode: (next: CameraTileStatusMode) => void;
  /** Kiosk mode (upstream #2531): pre-fetched statuses from the token feed.
   *  When given, the per-printer JWT status queries are skipped entirely — a
   *  kiosk has no session to run them with. */
  statusOverride?: Map<number, CameraWallStatus | undefined>;
  /** Kiosk mode: the token to authenticate tile streams with, since there is no
   *  logged-in browser to hold the usual short-lived stream token. */
  streamToken?: string;
  /** Hide the settings popover. A passive display has nobody standing at it, and
   *  its settings come from the URL rather than this browser's localStorage. */
  hideSettings?: boolean;
}

const MIN_SNAPSHOT_SEC = 2;
const MAX_SNAPSHOT_SEC = 60;
const STATUS_MODES: CameraTileStatusMode[] = ['off', 'compact', 'full'];

/** ⚠️ Module-level, not a `cameras = []` default parameter: that literal is a new
 *  array on every render, which would make `tiles` and the IntersectionObserver
 *  effect below rebuild every render — and since observing a tile sets state,
 *  that is an infinite render loop for every wall without cameras. */
const NO_CAMERAS: CameraWallCamera[] = [];

export function CameraWall({
  printers,
  cameras = NO_CAMERAS,
  snapshotIntervalSec,
  statusMode,
  onOpenPrinterCard,
  onChangeSnapshotIntervalSec,
  onChangeStatusMode,
  statusOverride,
  streamToken,
  hideSettings = false,
}: CameraWallProps) {
  const { t } = useTranslation();
  // Keyed by `sourceKey`, not by a printer id: two kinds of source share this
  // grid, and a number could only ever name one of them.
  const tileRefs = useRef<Map<string, HTMLDivElement | null>>(new Map());

  // Reuses the same ['printerStatus', id] cache that each PrinterCard
  // populates, so flipping between Cards and Cam Wall is instant. Disabled in
  // kiosk mode: that page has no JWT, so these would 401 on every poll — its
  // statuses arrive pre-fetched via statusOverride instead (upstream #2531).
  const kiosk = statusOverride != null;
  const statusQueries = useQueries({
    queries: (kiosk ? [] : printers).map((p) => ({
      queryKey: ['printerStatus', p.id],
      queryFn: () => api.getPrinterStatus(p.id),
      staleTime: 5000,
    })),
  });
  const fetchedStatusByPrinter = useMemo(() => {
    const map = new Map<number, PrinterStatus | undefined>();
    if (kiosk) return map;
    printers.forEach((p, i) => {
      map.set(p.id, statusQueries[i]?.data);
    });
    return map;
  }, [kiosk, printers, statusQueries]);
  const statusByPrinter: Map<number, CameraWallStatus | undefined> =
    statusOverride ?? fetchedStatusByPrinter;
  const [visibleKeys, setVisibleKeys] = useState<Set<string>>(() => new Set());
  // A wall is an overview first. A user explicitly selects the one camera
  // worth spending a live MJPEG connection on; all other visible tiles stay
  // on bounded snapshot refreshes.
  const [activeLiveKey, setActiveLiveKey] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const settingsRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!showSettings) return;
    const handler = (e: MouseEvent) => {
      if (settingsRef.current && !settingsRef.current.contains(e.target as Node)) {
        setShowSettings(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [showSettings]);

  // IntersectionObserver: a tile is "visible" when ≥40% of it is on-screen.
  // 40% (not 0%) avoids flicker at scroll boundaries where a tile is fractionally
  // visible — we don't want to spin up a live stream for a 5-pixel sliver.
  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        setVisibleKeys((prev) => {
          const next = new Set(prev);
          for (const entry of entries) {
            const key = (entry.target as HTMLElement).dataset.tileKey;
            if (!key) continue;
            if (entry.isIntersecting) next.add(key);
            else next.delete(key);
          }
          // Same set, same object: an observer that re-reports what we already
          // knew must not cost a render, let alone one per report.
          if (next.size === prev.size && [...next].every((key) => prev.has(key))) return prev;
          return next;
        });
      },
      { threshold: 0.4 },
    );

    for (const [, el] of tileRefs.current) {
      if (el) observer.observe(el);
    }
    return () => observer.disconnect();
  }, [printers, cameras]);

  // Scrolling a selected tile away is a navigation event, not a reason to keep
  // an invisible MJPEG viewer alive. The user can select it again after they
  // return to it; no printer state change ever selects a live camera for them.
  useEffect(() => {
    if (activeLiveKey !== null && !visibleKeys.has(activeLiveKey)) {
      setActiveLiveKey(null);
    }
  }, [activeLiveKey, visibleKeys]);

  // One list, printers first, then the standalone cameras by name. A camera
  // is "connected" when it is switched on: there is nothing else to ask.
  const tiles = useMemo<WallTile[]>(() => [
    ...printers.map((printer) => ({
      key: sourceKey(printerSource(printer.id)),
      source: printerSource(printer.id),
      name: printer.name,
      rotation: printer.camera_rotation,
      printer,
    })),
    ...[...cameras].sort((a, b) => a.name.localeCompare(b.name)).map((camera) => ({
      key: sourceKey(standaloneSource(camera.id)),
      source: standaloneSource(camera.id),
      name: camera.name,
      rotation: camera.rotation,
      printer: undefined,
    })),
  ], [printers, cameras]);

  const connectedOf = (tile: WallTile) =>
    tile.printer ? statusByPrinter.get(tile.printer.id)?.connected ?? false : true;

  const activeLiveIsConnected =
    activeLiveKey !== null && tiles.some((tile) => tile.key === activeLiveKey && connectedOf(tile));
  const requestedLive = activeLiveIsConnected ? 1 : 0;
  const { granted: liveSlots, ready, protocol, limit } = useCameraLiveBudget(requestedLive);

  const modeByKey = useMemo(() => {
    const map = new Map<string, CameraTileMode>();
    for (const tile of tiles) {
      const connected = tile.printer ? statusByPrinter.get(tile.printer.id)?.connected ?? false : true;
      if (!ready || !visibleKeys.has(tile.key) || !connected) {
        map.set(tile.key, 'paused');
        continue;
      }
      map.set(tile.key, tile.key === activeLiveKey && liveSlots > 0 ? 'live' : 'snapshot');
    }
    return map;
  }, [tiles, visibleKeys, liveSlots, ready, statusByPrinter, activeLiveKey]);

  const toggleLive = (tile: WallTile) => {
    if (!connectedOf(tile)) return;
    setActiveLiveKey((current) => (current === tile.key ? null : tile.key));
  };

  if (tiles.length === 0) {
    return (
      <div className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-4 text-center text-bambu-gray">
        {t('printers.camWall.noPrinters')}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {liveSlots < requestedLive && (
        <p className="text-xs text-bambu-gray" role="status">
          {t('printers.camWall.transportLimit', { protocol: protocol === 'unknown' ? t('printers.camWall.transportUnknown') : protocol, limit })}
        </p>
      )}
      <div className="flex items-center justify-between text-xs text-bambu-gray">
        <span>
          {t('printers.camWall.summary', {
            live: Array.from(modeByKey.values()).filter((m) => m === 'live').length,
            snap: Array.from(modeByKey.values()).filter((m) => m === 'snapshot').length,
            total: tiles.length,
          })}
        </span>
        <div className={`relative ${hideSettings ? 'hidden' : ''}`} ref={settingsRef}>
          <button
            type="button"
            onClick={() => setShowSettings((v) => !v)}
            className="flex h-7 items-center gap-1 rounded-md border border-bambu-dark-tertiary bg-bambu-dark px-2 text-white hover:bg-bambu-dark-tertiary"
            title={t('printers.camWall.settings.title')}
          >
            <SettingsIcon className="h-3.5 w-3.5" />
            <span>{t('printers.camWall.settings.title')}</span>
          </button>
          {showSettings && (
            <div className="absolute right-0 top-9 z-30 w-72 space-y-3 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary p-3 shadow-xl">
              <label className="block space-y-1">
                <span className="text-xs font-medium text-white">
                  {t('printers.camWall.settings.snapshotInterval')}
                </span>
                <input
                  type="number"
                  min={MIN_SNAPSHOT_SEC}
                  max={MAX_SNAPSHOT_SEC}
                  value={snapshotIntervalSec}
                  onChange={(e) => {
                    const n = Math.min(
                      MAX_SNAPSHOT_SEC,
                      Math.max(MIN_SNAPSHOT_SEC, Number(e.target.value) || MIN_SNAPSHOT_SEC),
                    );
                    onChangeSnapshotIntervalSec(n);
                  }}
                  className="w-full rounded-md border border-bambu-dark-tertiary bg-bambu-dark px-2 py-1 text-sm text-white"
                />
                <span className="block text-[11px] text-bambu-gray">
                  {t('printers.camWall.settings.snapshotIntervalHint')}
                </span>
              </label>
              <div className="space-y-1">
                <span className="block text-xs font-medium text-white">
                  {t('printers.camWall.settings.statusOverlay')}
                </span>
                <div
                  role="radiogroup"
                  aria-label={t('printers.camWall.settings.statusOverlay')}
                  className="flex overflow-hidden rounded-md border border-bambu-dark-tertiary"
                >
                  {STATUS_MODES.map((m) => (
                    <button
                      key={m}
                      type="button"
                      role="radio"
                      aria-checked={statusMode === m}
                      onClick={() => onChangeStatusMode(m)}
                      className={`flex-1 px-2 py-1 text-xs ${
                        statusMode === m
                          ? 'bg-bambu-green text-white font-semibold'
                          : 'bg-bambu-dark text-white hover:bg-bambu-dark-tertiary'
                      }`}
                    >
                      {t(`printers.camWall.statusMode.${m}`)}
                    </button>
                  ))}
                </div>
                <span className="block text-[11px] text-bambu-gray">
                  {t('printers.camWall.settings.statusOverlayHint')}
                </span>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {tiles.map((tile) => {
          const mode = modeByKey.get(tile.key) ?? 'paused';
          // Narrowed once, here: `tile.printer` inside a callback below is a
          // property access TypeScript cannot keep narrowed across the closure.
          const tilePrinter = tile.printer;
          const status = tilePrinter ? statusByPrinter.get(tilePrinter.id) : undefined;
          return (
            <div
              key={tile.key}
              ref={(el) => {
                tileRefs.current.set(tile.key, el);
              }}
              data-tile-key={tile.key}
            >
              <CameraTile
                source={tile.source}
                name={tile.name}
                cameraRotation={tile.rotation}
                mode={mode}
                snapshotIntervalMs={snapshotIntervalSec * 1000}
                connected={connectedOf(tile)}
                // A camera of its own has no print to report, so the whole
                // status overlay is simply absent rather than empty.
                statusMode={tilePrinter ? statusMode : 'off'}
                printerState={status?.state ?? null}
                progress={status?.progress ?? null}
                remainingMin={status?.remaining_time ?? null}
                layerNum={status?.layer_num ?? null}
                totalLayers={status?.total_layers ?? null}
                printName={
                  tilePrinter
                    ? cameraWallPrintName(status) ?? t('printers.camWall.currentJobFallback')
                    : null
                }
                hmsErrorCount={filterKnownHMSErrors(status?.hms_errors ?? []).length}
                streamToken={streamToken}
                activeLive={tile.key === activeLiveKey && mode === 'live'}
                // The kiosk wall deliberately receives neither handler: its
                // token can only view the passive, redacted wall.
                onToggleLive={onOpenPrinterCard ? () => toggleLive(tile) : undefined}
                // Only a printer has a card to open.
                onOpenPrinterCard={
                  onOpenPrinterCard && tilePrinter
                    ? () => onOpenPrinterCard(tilePrinter.id, tile.name)
                    : undefined
                }
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}
