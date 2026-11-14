/**
 * Where a camera view comes from: a printer's camera, or a camera of its own.
 *
 * The two answer the same question for a viewer — give me frames — and are
 * drawn by the same tile, the same floating window and the same full page. They
 * differ only in their URLs and in what else can be asked about them: a printer
 * has a status, a chamber light and a card; a camera in a room has none of that.
 *
 * Every path is built here rather than interpolated at each call site, because
 * the alternative is what this replaced: the same
 * `/api/v1/printers/${id}/camera/stream` template written out in the tile, the
 * window and the page, where a fourth kind of source means finding all three.
 */

export type CameraSourceKind = 'printer' | 'camera';

export interface CameraSource {
  kind: CameraSourceKind;
  id: number;
}

export const printerSource = (id: number): CameraSource => ({ kind: 'printer', id });
export const standaloneSource = (id: number): CameraSource => ({ kind: 'camera', id });

/** Stable key for React lists, refs and visibility sets across both kinds. */
export const sourceKey = (source: CameraSource): string => `${source.kind}-${source.id}`;

const base = (source: CameraSource): string =>
  source.kind === 'printer' ? `/api/v1/printers/${source.id}/camera` : `/api/v1/cameras/${source.id}`;

/**
 * A name for ONE stream a view opens — random, not a secret and not authority.
 *
 * Every viewer of a camera shares one upstream, so the server's `active` /
 * `stalled` cannot tell a view that ITS stream was ended (its authority
 * retired, its token expired) while somebody else still watches: nothing fires
 * on the `<img>`, and the last frame stays. The view sends this id on the
 * stream request and on its status read, and the status answers for it.
 */
export function newViewerId(): string {
  const bytes = new Uint8Array(12);
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  }
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/** `viewer` goes in HERE, before any token is appended to the address. */
export function streamPath(source: CameraSource, fps: number, bust?: number | string, viewer?: string): string {
  const cacheBust = bust === undefined ? '' : `&t=${bust}`;
  const viewerId = viewer ? `&viewer=${encodeURIComponent(viewer)}` : '';
  return `${base(source)}/stream?fps=${fps}${cacheBust}${viewerId}`;
}

/**
 * `poll` tells the server this is a repeating request at that cadence, so the
 * chamber light (when the farm asked for it) is held between frames instead of
 * blinking on each one. Only a printer has a chamber light, so only a printer's
 * frame carries it.
 */
export function snapshotPath(source: CameraSource, options: { bust?: number | string; pollMs?: number } = {}): string {
  const params = new URLSearchParams();
  if (options.bust !== undefined) params.set('t', String(options.bust));
  if (options.pollMs !== undefined && source.kind === 'printer') params.set('poll', String(options.pollMs));
  const query = params.toString();
  return `${base(source)}/snapshot${query ? `?${query}` : ''}`;
}

/**
 * Where a view asks whether its stream is alive, relative to the API base. Each kind answers for itself:
 * the printers' route knows printer ids only. With `viewer` — the id the view gave its stream — the answer
 * also says whether THAT stream is still attached.
 */
export const statusApiPath = (source: CameraSource, viewer?: string): string =>
  `${source.kind === 'printer' ? `/printers/${source.id}/camera/status` : `/cameras/${source.id}/status`}${
    viewer ? `?viewer=${encodeURIComponent(viewer)}` : ''
  }`;

export const stopPath = (source: CameraSource): string => `${base(source)}/stop`;

/** The full-page viewer's route for this source. */
export const viewerPath = (source: CameraSource): string =>
  source.kind === 'printer' ? `/camera/${source.id}` : `/camera/standalone/${source.id}`;

/**
 * Open the full-page viewer in a browser window, at the size and position the
 * last one was left at.
 *
 * Lives here because two callers need it — the camera button on a printer card
 * and the buttons a location's group header draws for its own cameras — and a
 * second copy of this feature string is how they would drift apart.
 */
export function openCameraWindow(source: CameraSource): void {
  let state: { width?: number; height?: number; left?: number; top?: number } = {};
  try {
    const saved = localStorage.getItem('cameraWindowState');
    if (saved) state = JSON.parse(saved);
  } catch {
    // A corrupt entry is not a reason to refuse to open the window.
  }
  const features = [
    `width=${state.width ?? 640}`,
    `height=${state.height ?? 400}`,
    state.left !== undefined ? `left=${state.left}` : '',
    state.top !== undefined ? `top=${state.top}` : '',
    'menubar=no,toolbar=no,location=no,status=no,noopener',
  ].filter(Boolean).join(',');
  window.open(viewerPath(source), `camera-${sourceKey(source)}`, features);
}
