/**
 * The ONE floating camera window, owned by the app shell.
 *
 * It used to live in the Printers page's state, so leaving the page closed it.
 * Here it stays open across pages: the provider is mounted once, in `Layout`,
 * and a route change neither remounts the viewer nor restarts its stream.
 *
 * ⚠️ One window, on purpose (not upstream's several overlays): opening another
 * camera REPLACES this one. A window that follows the user to every page also
 * holds a stream, the printer's single camera connection and the chamber-light
 * lease on every page, so it is kept honest here:
 * - it exists only inside the signed-in app (`Layout`) — never on the login
 *   page, a kiosk wall, the stream overlay or the camera page;
 * - it is checked against the live lists for as long as it is open, on
 *   whatever page that is, and closed and forgotten when its printer or camera
 *   is gone or `camera:view` is taken away;
 * - signing out forgets it, so the next sign-in does not reopen a stream;
 * - the viewer itself lets the stream go on a tab hidden for long
 *   (`CAMERA_HIDDEN_RELEASE_MS`).
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, getAuthToken } from '../api/client';
import { useAuth } from './AuthContext';
import { EmbeddedCameraViewer } from '../components/EmbeddedCameraViewer';
import {
  checkEmbeddedCamera, embeddedCameraName, readStoredEmbeddedCamera, storeEmbeddedCamera,
  type EmbeddedCameraSelection,
} from '../utils/embeddedCameraSelection';

interface EmbeddedCameraApi {
  /** Open this camera in the window — in place of whatever it shows now. */
  open: (selection: EmbeddedCameraSelection) => void;
  close: () => void;
}

/** How often an open window's list is read again when nothing else reads it —
 *  off the Printers page no card keeps the printer list fresh. */
export const EMBEDDED_CAMERA_RECHECK_MS = 60_000;

const EmbeddedCameraContext = createContext<EmbeddedCameraApi | null>(null);

// Outside the app shell there is no window to open. Nothing that opens one is
// mounted there (the Printers page lives under `Layout`), so this is a quiet
// stand-in, not a second owner.
const NO_WINDOW: EmbeddedCameraApi = { open: () => {}, close: () => {} };

export function useEmbeddedCamera(): EmbeddedCameraApi {
  return useContext(EmbeddedCameraContext) ?? NO_WINDOW;
}

export function EmbeddedCameraProvider({ children }: { children: ReactNode }) {
  // One owner per tree: a provider inside another one hands its children on.
  const outer = useContext(EmbeddedCameraContext);
  return outer ? <>{children}</> : <EmbeddedCameraOwner>{children}</EmbeddedCameraOwner>;
}

function EmbeddedCameraOwner({ children }: { children: ReactNode }) {
  // Read the old array format but restore only its last valid selection, so
  // upgrading cannot reopen a fleet of live connections.
  // ⚠️ `kind` distinguishes a printer's camera from a camera of its own: both
  // open the same window, and an id alone names neither.
  const [selection, setSelection] = useState<EmbeddedCameraSelection | null>(readStoredEmbeddedCamera);
  // ⚠️ What the browser remembered is not shown until it is vouched for: the
  // user may have lost `camera:view`, the printer may be gone (and its id
  // handed to the next one), the camera switched off. A camera opened by a
  // click needs no such check — it came off a live card.
  const [restoreUnchecked, setRestoreUnchecked] = useState(() => selection !== null);

  const open = useCallback((next: EmbeddedCameraSelection) => {
    setRestoreUnchecked(false);
    setSelection(next);
  }, []);
  const close = useCallback(() => setSelection(null), []);
  const vouched = useCallback(() => setRestoreUnchecked(false), []);

  useEffect(() => {
    storeEmbeddedCamera(selection);
  }, [selection]);

  // Signing out ends the app shell, and this window with it. Its stream closes
  // with the viewer; here the browser also forgets it, or the next sign-in
  // would reopen a stream nobody asked for.
  useEffect(() => () => {
    if (!getAuthToken()) storeEmbeddedCamera(null);
  }, []);

  const value = useMemo(() => ({ open, close }), [open, close]);

  return (
    <EmbeddedCameraContext.Provider value={value}>
      {children}
      {/* Mounted only while there is a window: without one the shell reads no
          list and holds nothing. */}
      {selection && (
        <OpenCameraWindow
          selection={selection}
          restoreUnchecked={restoreUnchecked}
          onVouched={vouched}
          onChange={setSelection}
        />
      )}
    </EmbeddedCameraContext.Provider>
  );
}

function OpenCameraWindow({ selection, restoreUnchecked, onVouched, onChange }: {
  selection: EmbeddedCameraSelection;
  restoreUnchecked: boolean;
  onVouched: () => void;
  onChange: (selection: EmbeddedCameraSelection | null) => void;
}) {
  const { loading: authLoading, hasPermission } = useAuth();

  // Only the list that can vouch for the open window is read. Same cache keys
  // as the Printers page and Settings.
  const { data: printers, isFetching: printersFetching } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
    enabled: selection.kind === 'printer',
    refetchInterval: EMBEDDED_CAMERA_RECHECK_MS,
  });
  const { data: cameras, isFetching: camerasFetching } = useQuery({
    queryKey: ['cameras'],
    queryFn: api.getCameras,
    enabled: selection.kind === 'camera',
    refetchInterval: EMBEDDED_CAMERA_RECHECK_MS,
  });

  // A list still being fetched cannot vouch for a remembered camera: the copy
  // in the cache may predate the printer's deletion. An OPEN window is checked
  // against every answer the lists give after that — its printer deleted, its
  // camera switched off or `camera:view` taken away while it is on screen
  // closes and forgets it — but is never hidden while a list is merely being
  // read again (that would restart its stream on every refetch). A list that
  // fails leaves a remembered window unshown and unforgotten until it answers.
  const verdict = authLoading
    ? 'wait'
    : checkEmbeddedCamera(selection, {
        canView: hasPermission('camera:view'),
        printers: restoreUnchecked && printersFetching ? undefined : printers,
        cameras: restoreUnchecked && camerasFetching ? undefined : cameras,
      });
  const shown = restoreUnchecked || verdict === 'drop' ? verdict : 'show';
  // The name the list gives the source now, not the one the browser kept.
  const name = embeddedCameraName(selection, { printers, cameras });
  useEffect(() => {
    if (verdict === 'wait') return;
    // Dropped silently: the stored entry goes with the selection.
    if (verdict === 'drop') {
      onChange(null);
    } else if (name && name !== selection.name) {
      onChange({ ...selection, name });
    }
    if (restoreUnchecked) onVouched();
  }, [selection, verdict, name, restoreUnchecked, onChange, onVouched]);

  if (shown !== 'show') return null;
  return (
    // The key unmounts/cancels the old camera before its replacement.
    <EmbeddedCameraViewer
      key={`${selection.kind}-${selection.id}`}
      source={{ kind: selection.kind, id: selection.id }}
      name={name || selection.name}
      onClose={() => onChange(null)}
    />
  );
}
