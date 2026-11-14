import { useCallback, useEffect, useMemo, useRef } from 'react';
import { useParams, useSearchParams } from 'react-router';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Layers, Clock, Timer, Printer, Flame, Square, Box } from 'lucide-react';
import { LoadingBlock } from '../components/LoadingBlock';
import { api, ApiError, withStreamToken } from '../api/client';
import { formatDuration, formatETA, type TimeFormat } from '../utils/date';
import { fillStyle, useRotatedBox } from '../hooks/useRotatedFit';
import { useCameraImageRef } from '../hooks/useCameraImageRef';
import { useCameraStreamRecovery, type CameraStreamHealth } from '../hooks/useCameraStreamRecovery';
import { printerSource, streamPath } from '../utils/cameraSource';
import { normalizeModelName } from '../utils/printer';
import {
  overlayDesign,
  overlayLayout,
  overlayTransparency,
  progressFill,
  type OverlayDesign,
  type OverlayLayout,
} from '../utils/overlayOptions';
import { StreamOverlayV2 } from '../components/StreamOverlayV2';
import { OverlayCanvas } from '../components/OverlayCanvas';

type TFunction = (key: string, options?: Record<string, unknown>) => string;

// Extract plate number from gcode_file path and append to print name
function formatPrintName(
  printName: string,
  gcodeFile: string | null | undefined,
  t: TFunction,
): string {
  if (!gcodeFile) return printName;
  const match = gcodeFile.match(/plate_(\d+)\.gcode/i);
  if (match && match[1] !== '1') {
    return `${printName} - ${t('printers.plateNumber', { number: match[1] })}`;
  }
  return printName;
}

type OverlaySize = 'small' | 'medium' | 'large';

interface OverlayConfig {
  size: OverlaySize;
  fps: number;
  showCamera: boolean;
  showProgress: boolean;
  showLayers: boolean;
  showEta: boolean;
  showFilename: boolean;
  showStatus: boolean;
  showPrinter: boolean;
  showModel: boolean;
  showNozzle: boolean;
  showBed: boolean;
  showChamber: boolean;
  progressFill: string | undefined;
  design: OverlayDesign;
  transparency: number;
  layout: OverlayLayout | null;
}

function parseConfig(params: URLSearchParams): OverlayConfig {
  const show = params.get('show')?.split(',') || ['progress', 'layers', 'eta', 'filename', 'status'];

  // Parse FPS (default 15, max 30, min 1)
  const fpsParam = parseInt(params.get('fps') || '15', 10);
  const fps = Math.min(Math.max(isNaN(fpsParam) ? 15 : fpsParam, 1), 30);

  // Parse camera toggle (default true, set camera=false to hide)
  const cameraParam = params.get('camera');
  const showCamera = cameraParam !== 'false' && cameraParam !== '0';

  return {
    size: (params.get('size') as OverlaySize) || 'medium',
    fps,
    showCamera,
    showProgress: show.includes('progress'),
    showLayers: show.includes('layers'),
    showEta: show.includes('eta'),
    showFilename: show.includes('filename'),
    showStatus: show.includes('status'),
    showPrinter: show.includes('printer'),
    // Independent of the name: `show=model` alone draws only the model.
    showModel: show.includes('model'),
    // ⚠️ Absent from the default set on purpose: temperatures are opt-in, so
    // every overlay URL already pasted into an OBS scene keeps looking exactly
    // the same after upgrading.
    showNozzle: show.includes('nozzle'),
    showBed: show.includes('bed'),
    showChamber: show.includes('chamber'),
    // Undefined unless the URL names a valid colour: the bar keeps its own.
    progressFill: progressFill(params.get('progressFrom'), params.get('progressTo')),
    // Classic unless the URL says `design=2`: every address already in a scene
    // keeps the design it was made with.
    design: overlayDesign(params.get('design')),
    transparency: overlayTransparency(params.get('backgroundTransparency')),
    // Null keeps the overlay sized by its window.
    layout: overlayLayout(params.get('layout')),
  };
}

// Accepts the minimal shape shared by PrinterStatus (logged-in path) and the
// token-authed OverlayStatus (kiosk path) — both carry state + stg_cur_name.
function getStatusText(status: { state: string | null; stg_cur_name?: string | null }, t: TFunction): string {
  if (status.stg_cur_name) return status.stg_cur_name;

  switch (status.state) {
    case 'RUNNING': return t('streamOverlay.status.printing');
    case 'PAUSE': return t('streamOverlay.status.paused');
    case 'FINISH': return t('streamOverlay.status.finished');
    case 'FAILED': return t('streamOverlay.status.failed');
    case 'IDLE': return t('streamOverlay.status.idle');
    default: return status.state || t('streamOverlay.status.unknown');
  }
}

// Reads one reading out of either status shape. The token-authed overlay feed
// types temperatures as Record<string, number>; the logged-in PrinterStatus
// types it as a named object that also carries `*_heating` booleans. Narrowing
// here lets one render path serve both without casting.
function readTemp(temps: Record<string, unknown>, key: string): number | null {
  const value = temps[key];
  return typeof value === 'number' ? value : null;
}

interface TempReadingProps {
  icon: React.ReactNode;
  label: string;
  current: number;
  target: number | null;
  sizes: ReturnType<typeof getSizeClasses>;
}

// One "Nozzle 220°C" reading. ⚠️ The target is appended only while it is set
// and still differs from the current value, so a hotend that has reached
// temperature reads "220°C" for the rest of the print instead of the noisier
// "220 / 220°C" — 219.6 against a target of 220 rounds to the same number, and
// repeating it says nothing.
function TempReading({ icon, label, current, target, sizes }: TempReadingProps) {
  const heating = target != null && target > 0 && Math.round(target) !== Math.round(current);
  return (
    <div className={`flex items-center ${sizes.gap} text-white/70`}>
      {icon}
      <span className={sizes.text}>
        <span className="mr-1">{label}</span>
        <span className="text-white">{Math.round(current)}°C</span>
        {heating && (
          <>
            <span className="mx-1">/</span>
            <span>{Math.round(target)}°C</span>
          </>
        )}
      </span>
    </div>
  );
}

// Classic on the portrait canvas (`layout=portrait`): the canvas is 1080 wide
// and usually shown small — a phone-shaped stream — so the text is larger, and
// a long file name wraps instead of being cut after a few characters.
function getPortraitSizeClasses(size: OverlaySize) {
  switch (size) {
    case 'small':
      return {
        container: 'p-6',
        text: 'text-2xl',
        textLarge: 'text-3xl',
        progressHeight: 'h-3',
        icon: 'w-6 h-6 shrink-0',
        gap: 'gap-3',
        logoHeight: 'h-24',
        filename: 'break-words',
      };
    case 'large':
      return {
        container: 'p-8',
        text: 'text-4xl',
        textLarge: 'text-5xl',
        progressHeight: 'h-5',
        icon: 'w-9 h-9 shrink-0',
        gap: 'gap-5',
        logoHeight: 'h-32',
        filename: 'break-words',
      };
    case 'medium':
    default:
      return {
        container: 'p-8',
        text: 'text-3xl',
        textLarge: 'text-4xl',
        progressHeight: 'h-4',
        icon: 'w-8 h-8 shrink-0',
        gap: 'gap-4',
        logoHeight: 'h-28',
        filename: 'break-words',
      };
  }
}

function getSizeClasses(size: OverlaySize, portrait = false) {
  if (portrait) return getPortraitSizeClasses(size);
  switch (size) {
    case 'small':
      return {
        container: 'p-3',
        text: 'text-sm',
        textLarge: 'text-lg',
        progressHeight: 'h-2',
        icon: 'w-3 h-3',
        gap: 'gap-2',
        logoHeight: 'h-12',
        filename: 'truncate',
      };
    case 'large':
      return {
        container: 'p-4',
        text: 'text-xl',
        textLarge: 'text-3xl',
        progressHeight: 'h-4',
        icon: 'w-6 h-6',
        gap: 'gap-4',
        logoHeight: 'h-24',
        filename: 'truncate',
      };
    case 'medium':
    default:
      return {
        container: 'p-4',
        text: 'text-base',
        textLarge: 'text-xl',
        progressHeight: 'h-3',
        icon: 'w-4 h-4',
        gap: 'gap-3',
        logoHeight: 'h-16',
        filename: 'truncate',
      };
  }
}

export function StreamOverlayPage() {
  const { printerId } = useParams<{ printerId: string }>();
  const [searchParams] = useSearchParams();
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const id = parseInt(printerId || '0', 10);

  const config = useMemo(() => parseConfig(searchParams), [searchParams]);
  const sizes = getSizeClasses(config.size, config.layout === 'portrait');

  // Kiosk mode (upstream #2613): OBS and other embeds have no login session, so
  // they pass an `overlay`-scoped token in the URL. When present, every data
  // call (status + camera stream) is authenticated by that token instead of a
  // JWT. Without this the overlay simply rendered blank whenever it was loaded
  // outside a signed-in browser — everything it draws is auth-gated.
  const token = searchParams.get('token');
  const kiosk = token != null && token !== '';

  // The id of the stream this page has open (set below, once the recovery
  // hook has named it), and the id the feed's latest answer was asked about.
  const streamViewer = useRef<string | undefined>(undefined);
  const feedAskedAbout = useRef<string | undefined>(undefined);

  // Kiosk path: one token-authenticated call for name + live status + the one
  // setting the overlay reads. No JWT, so this is the only feed available.
  const { data: overlay, error: overlayError, dataUpdatedAt: overlayReadAt } = useQuery({
    queryKey: ['overlayStatus', id, token],
    queryFn: async () => {
      const viewer = streamViewer.current;
      const answer = await (viewer ? api.getOverlayStatus(id, token!, viewer) : api.getOverlayStatus(id, token!));
      feedAskedAbout.current = viewer;
      return answer;
    },
    enabled: id > 0 && kiosk,
    retry: (attempt, error) => !(error instanceof ApiError && (error.status === 401 || error.status === 403)) && attempt < 3,
    retryDelay: attempt => Math.min(30_000, 1000 * 2 ** attempt),
    refetchInterval: query => query.state.error instanceof ApiError && (query.state.error.status === 401 || query.state.error.status === 403)
      ? false : Math.min(30_000, 2000 * 2 ** Math.min(query.state.fetchFailureCount, 4)),
  });

  // Logged-in path: the ordinary JWT-authenticated queries, unchanged. Disabled
  // in kiosk mode so an unauthenticated OBS browser never fires a doomed 401.
  const { data: printerData } = useQuery({
    queryKey: ['printer', id],
    queryFn: () => api.getPrinter(id),
    enabled: id > 0 && !kiosk,
  });

  const { data: statusData } = useQuery({
    queryKey: ['printerStatus', id],
    queryFn: () => api.getPrinterStatus(id),
    enabled: id > 0 && !kiosk,
    refetchInterval: 2000,
  });

  const { data: settings } = useQuery({
    queryKey: ['ui-preferences'],
    queryFn: api.getUiPreferences,
    enabled: !kiosk,
  });

  // Normalize the two sources into the shape the render below reads. Memoized
  // because the title effect depends on `printer` — a fresh object literal each
  // render would re-run it (and reset document.title) on every poll tick.
  const printer = useMemo(
    () =>
      kiosk
        ? overlay && { name: overlay.name, model: overlay.model, camera_rotation: overlay.camera_rotation }
        : printerData,
    [kiosk, overlay, printerData],
  );
  const status = kiosk ? overlay : statusData;
  // The model by its display name, through OUR map: the row may hold an
  // internal code or the long marketing name. The token feed already sends the
  // display name; passing it through again costs nothing. Empty when the URL
  // does not ask for it or an older server's feed does not name it.
  const modelName = config.showModel ? normalizeModelName(printer?.model) : '';

  // Temperature readings the URL asked for, in a fixed order, skipping any the
  // printer does not report. Labels reuse printers.heaterHistory.* so the
  // naming matches the heater chart rather than inventing a second vocabulary
  // for the same three things.
  const temps: Record<string, unknown> = (status?.temperatures ?? {}) as Record<string, unknown>;
  const tempReadings: {
    key: string;
    icon: React.ReactNode;
    label: string;
    current: number;
    target: number | null;
  }[] = [];
  if (config.showNozzle) {
    const nozzle = readTemp(temps, 'nozzle');
    const nozzle2 = readTemp(temps, 'nozzle_2');
    if (nozzle != null) {
      tempReadings.push({
        key: 'nozzle',
        icon: <Flame className={sizes.icon} />,
        label: t('printers.heaterHistory.nozzle'),
        current: nozzle,
        target: readTemp(temps, 'nozzle_target'),
      });
    }
    if (nozzle2 != null) {
      tempReadings.push({
        key: 'nozzle_2',
        icon: <Flame className={sizes.icon} />,
        label: t('printers.heaterHistory.nozzle2'),
        current: nozzle2,
        target: readTemp(temps, 'nozzle_2_target'),
      });
    }
  }
  if (config.showBed) {
    const bed = readTemp(temps, 'bed');
    if (bed != null) {
      tempReadings.push({
        key: 'bed',
        icon: <Square className={sizes.icon} />,
        label: t('printers.heaterHistory.bed'),
        current: bed,
        target: readTemp(temps, 'bed_target'),
      });
    }
  }
  if (config.showChamber) {
    const chamber = readTemp(temps, 'chamber');
    if (chamber != null) {
      tempReadings.push({
        key: 'chamber',
        icon: <Box className={sizes.icon} />,
        label: t('printers.heaterHistory.chamber'),
        current: chamber,
        target: readTemp(temps, 'chamber_target'),
      });
    }
  }
  const timeFormat: TimeFormat = (kiosk ? overlay?.time_format : settings?.time_format) || 'system';

  // WebSocket for real-time updates
  useEffect(() => {
    // Skipped in kiosk mode: an overlay token can't mint a ws-token, and the 2s
    // poll above is the feed there (upstream #2613).
    if (!id || kiosk) return;

    let ws: WebSocket | null = null;
    let cancelled = false;

    // GHSA-r2qv follow-up: /api/v1/ws requires a short-lived token (the HTTP
    // auth middleware can't gate the WebSocket upgrade). Mint one before
    // connecting via api.getWebSocketToken so the JWT Authorization header
    // rides along (a raw tokenless socket is closed 4401 by the server). An
    // auth-disabled deployment succeeds even without a token.
    (async () => {
      let token: string | undefined;
      try {
        const resp = await api.getWebSocketToken();
        token = resp.token;
      } catch (err) {
        // A 401 (JWT expired) / 403 (no WEBSOCKET_CONNECT permission) is an
        // auth decision — a tokenless socket would just be closed 4401, so
        // skip opening one and let the REST polling fallback keep the overlay
        // fresh. There's no reconnect loop on this page, so this is purely
        // avoiding one doomed socket per mount. A network/5xx error is not
        // auth: fall through and try anyway (auth-disabled deployments land
        // here with no token and connect fine).
        const status = err instanceof ApiError ? err.status : 0;
        if (status === 401 || status === 403) return;
      }
      if (cancelled) return;

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const tokenParam = token ? `?token=${encodeURIComponent(token)}` : '';
      const wsUrl = `${protocol}//${window.location.host}/api/v1/ws${tokenParam}`;
      ws = new WebSocket(wsUrl);

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'printer_status' && data.printer_id === id) {
            queryClient.setQueryData(['printerStatus', id], data.status);
          }
        } catch {
          // Ignore parse errors
        }
      };

      ws.onerror = () => {
        // WebSocket error - polling will continue as fallback
      };
    })();

    return () => {
      cancelled = true;
      if (ws) ws.close();
    };
  }, [id, kiosk, queryClient]);

  // Update document title
  useEffect(() => {
    document.title = printer ? `${printer.name} - ${t('streamOverlay.title')}` : t('streamOverlay.title');
    return () => {
      document.title = 'BamDude';
    };
  }, [printer, t]);

  const kioskRefused = kiosk && overlayError instanceof ApiError && (overlayError.status === 401 || overlayError.status === 403);

  // Is the stream this page draws still alive? Asked of the SERVER, because a
  // stream it ends cleanly fires nothing on the image and the broadcast would
  // show a frozen frame until somebody reloaded the source.
  // - OBS (token in the URL): the token cannot read the session's camera status
  //   route, so the answer rides the feed this page already polls with that
  //   token (`camera`). Each answer is read once: while the feed is away, the
  //   last one must not count as a second bad read. A server that does not
  //   send the field leaves only the image's own `error`.
  // - signed in: the ordinary camera status route.
  // Either way the read names the stream this page has open, because the
  // upstream is shared: another overlay of the same printer, or its owner's
  // camera wall, keeps it "alive" after the server has ended THIS viewer.
  const feedCamera = useRef<{ health: CameraStreamHealth | undefined; at: number; viewer: string | undefined }>(
    { health: undefined, at: 0, viewer: undefined },
  );
  const feedCameraRead = useRef(0);
  useEffect(() => {
    feedCamera.current = { health: overlay?.camera, at: overlayReadAt, viewer: feedAskedAbout.current };
  }, [overlay, overlayReadAt]);
  const readStreamHealth = useCallback(async (viewerId: string): Promise<CameraStreamHealth | null> => {
    if (!kiosk) return api.getCameraSourceStatus(printerSource(id), viewerId);
    const { health, at, viewer } = feedCamera.current;
    if (!health || at === feedCameraRead.current) return null;
    feedCameraRead.current = at;
    // An answer asked about the stream before this one says nothing of this one.
    return viewer === viewerId ? health : { active: health.active, stalled: health.stalled };
  }, [kiosk, id]);

  // A refused token ends it: nothing is retried once the feed says 401/403.
  // Nobody can press Retry on a broadcast, so after its bounded attempts the
  // overlay shows "No signal" and tries again once a minute.
  const showsCamera = id > 0 && config.showCamera && !kioskRefused && !!status;
  const {
    phase: streamPhase,
    generation: streamGeneration,
    viewerId: streamViewerId,
    onError: handleStreamError,
    onLoad: handleStreamLoad,
  } = useCameraStreamRecovery({ enabled: showsCamera, readHealth: readStreamHealth, slowRetryMs: 60_000 });
  useEffect(() => {
    streamViewer.current = config.showCamera ? streamViewerId : undefined;
  }, [config.showCamera, streamViewerId]);

  // Append the kiosk token directly rather than leaning on withStreamToken's
  // module cache — the cache is populated by an effect and would miss the first
  // render (a 401 flash before the retry). The logged-in path keeps the cache.
  const camPath = streamPath(printerSource(id), config.fps, streamGeneration, streamViewerId);
  const streamUrl = kiosk && token
    ? `${camPath}&token=${encodeURIComponent(token)}`
    : withStreamToken(camPath);
  // The mounted image gets its URL from the hook, never as a prop, and loses
  // it when it goes: OBS is Chromium, and a dropped <img> keeps its request.
  const { attachImage: attachStreamImage } = useCameraImageRef(streamUrl);

  // The picture fills the browser source: a sideways camera is laid out in
  // the source's sides swapped, or it is cropped. The box appears once the
  // status has loaded; the hook picks it up then.
  const { boxRef: sceneRef, box: scene } = useRotatedBox(printer?.camera_rotation);

  if (!id) {
    return (
      <div className="min-h-screen bg-black flex items-center justify-center">
        <p className="text-white">{t('streamOverlay.invalidPrinterId')}</p>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="min-h-screen bg-black flex items-center justify-center">
        {/* Lighter than the page default: this is composited into somebody's
            broadcast, so it stays quiet — the spinner is the whole point, the
            chrome around it is not. */}
        <LoadingBlock label={t('common.loading')} className="text-gray-400" />
      </div>
    );
  }

  // ⚠️ A dropped MQTT session flips only `connected`: the server keeps answering
  // with the last state, progress, layers, ETA and temperatures it cached. On a
  // broadcast those would sit frozen and look live, so an offline printer shows
  // the offline line and none of them. The file name stays — it names the job
  // that was running, which is still true.
  const online = status.connected;
  const isPrinting = online && (status.state === 'RUNNING' || status.state === 'PAUSE');
  const progress = status.progress || 0;

  // `layout=` draws the overlay on a fixed canvas scaled to the browser
  // source; without it the overlay is sized by its window, as it always was.
  const framed = (body: React.ReactElement, ground: string) =>
    config.layout ? <OverlayCanvas layout={config.layout} ground={ground}>{body}</OverlayCanvas> : body;

  if (config.design === 'v2') {
    // The same facts as Classic below, handed over as values: what must not be
    // shown (an offline printer's frozen figures) is null, and Version 2 only
    // draws. The status names the offline printer itself — this design has no
    // separate idle line.
    const remaining = isPrinting && config.showEta && status.remaining_time != null && status.remaining_time > 0
      ? status.remaining_time
      : null;
    // Around a canvas in a source of another shape: the same ground the
    // design itself has, see-through when the URL asks for it.
    const alpha = 1 - config.transparency / 100;
    return framed(
      <StreamOverlayV2
        layout={config.layout}
        size={config.size}
        transparency={config.transparency}
        progressFill={config.progressFill}
        camera={showsCamera ? {
          attach: attachStreamImage,
          generation: streamGeneration,
          rotation: printer?.camera_rotation ?? 0,
          phase: streamPhase,
          onError: handleStreamError,
          onLoad: handleStreamLoad,
        } : null}
        name={config.showPrinter && printer ? printer.name : null}
        model={modelName || null}
        filename={config.showFilename && status.current_print
          ? formatPrintName(status.current_print.replace(/\.gcode\.3mf$|\.3mf$|\.gcode$/i, ''), status.gcode_file, t)
          : null}
        status={config.showStatus ? (online ? getStatusText(status, t) : t('streamOverlay.printerOffline')) : null}
        state={online ? status.state : 'OFFLINE'}
        progress={config.showProgress && isPrinting ? Math.min(Math.max(progress, 0), 100) : null}
        layers={config.showLayers && isPrinting && status.layer_num != null && status.total_layers != null && status.total_layers > 0
          ? `${status.layer_num} / ${status.total_layers}`
          : null}
        remaining={remaining != null ? formatDuration(remaining * 60) : null}
        finish={remaining != null ? formatETA(remaining, timeFormat, t) : null}
        temperatures={online ? tempReadings : []}
      />,
      alpha === 0 ? 'transparent' : `rgba(0, 0, 0, ${alpha})`,
    );
  }

  return framed(
    <div ref={sceneRef} className={`${config.layout ? 'h-full' : 'min-h-screen'} bg-black relative overflow-hidden`}>
      {/* Camera feed - fullscreen background (optional) */}
      {showsCamera && streamPhase === 'live' && (
        <img
          ref={attachStreamImage}
          key={streamGeneration}
          alt={t('streamOverlay.cameraStream')}
          className="absolute inset-0 w-full h-full object-contain"
          style={fillStyle(scene, printer?.camera_rotation)}
          onError={handleStreamError}
          onLoad={handleStreamLoad}
        />
      )}
      {showsCamera && streamPhase === 'failed' && (
        <div className="absolute inset-0 flex items-center justify-center">
          <p className={`${sizes.text} text-white/60`}>{t('streamOverlay.noSignal')}</p>
        </div>
      )}

      {/* BamDude logo - top right */}
      <a
        href="https://github.com/kainpl/bamdude"
        target="_blank"
        rel="noopener noreferrer"
        className="absolute top-4 right-4 z-10"
      >
        <img
          src="/img/brand/lockup-compact-on-dark.svg"
          alt="BamDude"
          className={`${sizes.logoHeight} w-auto object-contain drop-shadow-lg hover:scale-105 transition-transform`}
        />
      </a>

      {/* Status overlay - bottom */}
      <div className="absolute bottom-0 left-0 right-0 z-10 bg-gradient-to-t from-black/80 via-black/60 to-transparent">
        <div className={`${sizes.container}`}>
          {/* Printer name */}
          {printer && (config.showPrinter || modelName) && (
            <div className={`flex items-center ${sizes.gap} mb-2`}>
              <Printer className={`${sizes.icon} text-white/70`} />
              {config.showPrinter && (
                <span className={`${sizes.text} text-white font-medium`}>{printer.name}</span>
              )}
              {config.showPrinter && modelName && <span className={`${sizes.text} text-white/50`}>·</span>}
              {modelName && <span className={`${sizes.text} text-white/70`}>{modelName}</span>}
            </div>
          )}

          {/* Filename */}
          {config.showFilename && status.current_print && (
            <div className={`${sizes.textLarge} text-white font-semibold mb-2 ${sizes.filename} drop-shadow-md`}>
              {formatPrintName(status.current_print.replace(/\.gcode\.3mf$|\.3mf$|\.gcode$/i, ''), status.gcode_file, t)}
            </div>
          )}

          {/* Status text */}
          {config.showStatus && online && (
            <div className={`${sizes.text} text-white/70 mb-2`}>
              {getStatusText(status, t)}
            </div>
          )}

          {/* Progress bar */}
          {config.showProgress && isPrinting && (
            <div className="mb-3">
              <div className={`flex items-center justify-between mb-1 ${sizes.text}`}>
                <span className="text-white/70">{t('streamOverlay.progress')}</span>
                <span className="text-white font-bold">{Math.round(progress)}%</span>
              </div>
              <div className={`w-full bg-white/20 rounded-full ${sizes.progressHeight}`}>
                <div
                  className={`bg-bambu-green ${sizes.progressHeight} rounded-full transition-all duration-500`}
                  style={{
                    width: `${progress}%`,
                    ...(config.progressFill ? { backgroundImage: config.progressFill } : {}),
                  }}
                />
              </div>
            </div>
          )}

          {/* Stats row */}
          {isPrinting && (config.showLayers || config.showEta) && (
            <div className={`flex items-center ${sizes.gap} flex-wrap`}>
              {/* Layers */}
              {config.showLayers && status.layer_num != null && status.total_layers != null && status.total_layers > 0 && (
                <div className={`flex items-center ${sizes.gap} text-white/70`}>
                  <Layers className={sizes.icon} />
                  <span className={sizes.text}>
                    <span className="text-white">{status.layer_num}</span>
                    <span className="mx-1">/</span>
                    <span>{status.total_layers}</span>
                  </span>
                </div>
              )}

              {/* Remaining time */}
              {config.showEta && status.remaining_time != null && status.remaining_time > 0 && (
                <>
                  <div className={`flex items-center ${sizes.gap} text-white/70`}>
                    <Timer className={sizes.icon} />
                    <span className={`${sizes.text} text-white`}>
                      {formatDuration(status.remaining_time * 60)}
                    </span>
                  </div>

                  <div className={`flex items-center ${sizes.gap} text-white/70`}>
                    <Clock className={sizes.icon} />
                    <span className={`${sizes.text} text-white`}>
                      {t('streamOverlay.eta')} {formatETA(status.remaining_time, timeFormat, t)}
                    </span>
                  </div>
                </>
              )}
            </div>
          )}

          {/* Temperatures. ⚠️ Rendered whether or not a print is running — a
              preheating or cooling printer is exactly when these are worth
              watching. Each reading appears only when the printer reports it,
              so a single-nozzle machine shows one nozzle, and a model without a
              chamber sensor shows no chamber row even with `chamber` in
              ?show= (the backend omits the reading entirely for those). */}
          {online && tempReadings.length > 0 && (
            <div className={`flex items-center ${sizes.gap} flex-wrap mt-2`}>
              {tempReadings.map((reading) => (
                <TempReading
                  key={reading.key}
                  icon={reading.icon}
                  label={reading.label}
                  current={reading.current}
                  target={reading.target}
                  sizes={sizes}
                />
              ))}
            </div>
          )}

          {/* Idle state */}
          {!isPrinting && (
            <div className={`${sizes.text} text-white/70 py-2`}>
              {status.connected ? t('streamOverlay.printerIdle') : t('streamOverlay.printerOffline')}
            </div>
          )}
        </div>
      </div>
    </div>,
    '#000',
  );
}
