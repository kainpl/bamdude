/**
 * Standalone Cam Wall at /camwall (upstream #2531).
 *
 * The wall inside the app is a toggle on the Printers page, so it had no URL —
 * it could not be bookmarked, linked, or pinned to a wall-mounted screen. This
 * page is that URL.
 *
 * Two modes:
 *   - signed in: the wall exactly as it was, on the ordinary printers API.
 *   - `?token=…`: a TV or Pi with no login. Every call is authenticated by a
 *     long-lived `camwall`-scoped token instead of a JWT — the printer list
 *     comes from the read-only kiosk feed (which serves neither serial number
 *     nor IP address, and never names the file being printed), and the tiles
 *     stream with the same token.
 */
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, type CamWallPrinter } from '../api/client';
import { CameraWall, type CameraWallStatus } from '../components/CameraWall';
import type { CameraTileStatusMode } from '../components/CameraTile';

// Same localStorage keys the in-app wall uses, so a kiosk started from a
// browser that had already tuned the wall inherits those settings.
const MAX_LIVE_KEY = 'camWallMaxLive';
const SNAPSHOT_SEC_KEY = 'camWallSnapshotSec';
const STATUS_MODE_KEY = 'camWallStatusMode';

const DEFAULT_MAX_LIVE = 4;
const DEFAULT_SNAPSHOT_SEC = 10;

// A kiosk has no WebSocket to invalidate its queries, so it polls. Matched to
// the in-app wall's staleTime rather than something faster: nobody is
// interacting with a wall, and every tick is N printers' worth of state.
const KIOSK_POLL_MS = 5000;

const MIN_MAX_LIVE = 1;
const MAX_MAX_LIVE = 16;
const MIN_SNAPSHOT_SEC = 2;
const MAX_SNAPSHOT_SEC = 60;

function readNumber(key: string, fallback: number): number {
  const saved = parseInt(localStorage.getItem(key) || '', 10);
  return Number.isFinite(saved) && saved > 0 ? saved : fallback;
}

/** A URL parameter wins over localStorage, and is clamped to the same range the
 *  settings popover enforces. A kiosk browser is awkward to reach — you can't
 *  open devtools on a wall-mounted TV to set a localStorage key — so the URL is
 *  the only practical way to configure one. Out-of-range or unparseable values
 *  fall back rather than producing a wall nobody can fix from the same URL. */
function paramNumber(raw: string | null, min: number, max: number, fallback: number): number {
  const n = parseInt(raw ?? '', 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(Math.max(n, min), max);
}

export function CamWallPage() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const token = searchParams.get('token');
  const kiosk = token != null && token !== '';
  // Same value, read once for the state initialisers below (they run before
  // `kiosk` is in scope in source order, but not in execution order — kept
  // separate so the intent is explicit rather than relying on hoisting).
  const kioskFromUrl = kiosk;

  const [maxLive, setMaxLive] = useState(() =>
    paramNumber(
      searchParams.get('maxLive'),
      MIN_MAX_LIVE,
      MAX_MAX_LIVE,
      readNumber(MAX_LIVE_KEY, DEFAULT_MAX_LIVE),
    ),
  );
  const [snapshotSec, setSnapshotSec] = useState(() =>
    paramNumber(
      searchParams.get('interval'),
      MIN_SNAPSHOT_SEC,
      MAX_SNAPSHOT_SEC,
      readNumber(SNAPSHOT_SEC_KEY, DEFAULT_SNAPSHOT_SEC),
    ),
  );
  const [statusMode, setStatusMode] = useState<CameraTileStatusMode>(() => {
    const fromUrl = searchParams.get('status');
    // 'full' is deliberately not selectable on a token wall: the kiosk feed does
    // not serve the print filename at all, so 'full' would render gaps — and the
    // whole point of withholding it is that a screen in a shared room never
    // names the part on the bed.
    if (fromUrl === 'off' || fromUrl === 'compact') return fromUrl;
    if (fromUrl === 'full' && !kioskFromUrl) return 'full';
    const saved = localStorage.getItem(STATUS_MODE_KEY);
    if (saved === 'off' || saved === 'compact' || (saved === 'full' && !kioskFromUrl)) return saved;
    // Compact is the right default for a passive display.
    return 'compact';
  });

  // Persist only on the signed-in wall. A kiosk's settings come from its URL, and
  // writing them back would let opening a kiosk link once silently overwrite the
  // wall preferences of whoever's browser it was opened in.
  useEffect(() => {
    if (!kiosk) localStorage.setItem(MAX_LIVE_KEY, String(maxLive));
  }, [kiosk, maxLive]);
  useEffect(() => {
    if (!kiosk) localStorage.setItem(SNAPSHOT_SEC_KEY, String(snapshotSec));
  }, [kiosk, snapshotSec]);
  useEffect(() => {
    if (!kiosk) localStorage.setItem(STATUS_MODE_KEY, statusMode);
  }, [kiosk, statusMode]);

  // Kiosk path: one token-authenticated call for the whole wall.
  const { data: kioskPrinters, isError: kioskError } = useQuery({
    queryKey: ['camwallPrinters', token],
    queryFn: () => api.getCamWallPrinters(token!),
    enabled: kiosk,
    refetchInterval: KIOSK_POLL_MS,
  });

  // Signed-in path: the ordinary printer list, unchanged. Disabled in kiosk
  // mode so an unauthenticated screen never fires a doomed 401.
  const { data: authedPrinters } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
    enabled: !kiosk,
  });

  const printers = kiosk ? (kioskPrinters ?? []) : (authedPrinters ?? []);

  // In kiosk mode the statuses arrive with the list, so hand them to the wall
  // rather than letting it run its own per-printer JWT queries.
  const statusOverride = useMemo(() => {
    if (!kiosk) return undefined;
    const map = new Map<number, CameraWallStatus | undefined>();
    (kioskPrinters ?? []).forEach((p) => map.set(p.id, p));
    return map;
  }, [kiosk, kioskPrinters]);

  useEffect(() => {
    document.title = t('camWall.pageTitle');
  }, [t]);

  if (kiosk && kioskError) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-bambu-dark p-8 text-center">
        <p className="max-w-md text-sm text-bambu-gray">{t('camWall.tokenRejected')}</p>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-bambu-dark p-4">
      <CameraWall
        printers={printers}
        maxLive={maxLive}
        snapshotIntervalSec={snapshotSec}
        statusMode={statusMode}
        onChangeMaxLive={setMaxLive}
        onChangeSnapshotIntervalSec={setSnapshotSec}
        onChangeStatusMode={setStatusMode}
        statusOverride={statusOverride}
        streamToken={kiosk ? (token ?? undefined) : undefined}
        // A passive display has nobody standing at it, and its settings come
        // from the URL rather than this browser's localStorage.
        hideSettings={kiosk}
        // No tile handler in kiosk mode: the token cannot open the
        // single-camera view, so a clickable-looking tile would just be a lie.
        onTileClick={
          kiosk
            ? undefined
            : (printerId) => {
                window.location.href = `/camera/${printerId}`;
              }
        }
      />
    </div>
  );
}

export type { CamWallPrinter };
