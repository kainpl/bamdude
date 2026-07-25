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

function readNumber(key: string, fallback: number): number {
  const saved = parseInt(localStorage.getItem(key) || '', 10);
  return Number.isFinite(saved) && saved > 0 ? saved : fallback;
}

export function CamWallPage() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const token = searchParams.get('token');
  const kiosk = token != null && token !== '';

  const [maxLive, setMaxLive] = useState(() => readNumber(MAX_LIVE_KEY, DEFAULT_MAX_LIVE));
  const [snapshotSec, setSnapshotSec] = useState(() =>
    readNumber(SNAPSHOT_SEC_KEY, DEFAULT_SNAPSHOT_SEC),
  );
  const [statusMode, setStatusMode] = useState<CameraTileStatusMode>(() => {
    const saved = localStorage.getItem(STATUS_MODE_KEY);
    // A token wall renders the compact overlay: the feed doesn't serve the print
    // filename, so 'full' would just show gaps. Compact is also the right
    // default for a screen in a shared room.
    if (saved === 'off' || saved === 'compact' || saved === 'full') return saved;
    return 'compact';
  });

  useEffect(() => {
    localStorage.setItem(MAX_LIVE_KEY, String(maxLive));
  }, [maxLive]);
  useEffect(() => {
    localStorage.setItem(SNAPSHOT_SEC_KEY, String(snapshotSec));
  }, [snapshotSec]);
  useEffect(() => {
    localStorage.setItem(STATUS_MODE_KEY, statusMode);
  }, [statusMode]);

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
