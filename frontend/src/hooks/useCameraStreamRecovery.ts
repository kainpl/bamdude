import { useCallback, useEffect, useRef, useState } from 'react';
import { newViewerId } from '../utils/cameraSource';

/** What the server says about one source's stream. */
export interface CameraStreamHealth {
  /** The upstream every viewer of the source shares is registered… */
  active: boolean;
  /** …and has stopped delivering. */
  stalled: boolean;
  /** Whether THIS view's stream (by the id it sent) is still attached.
   *  `false` is a bad read; absent or `null` is "cannot say" and decides
   *  nothing — an older server does not send it. */
  viewer_attached?: boolean | null;
}

/** The id of the stream a view has open: a new one each time `streamKey` — the
 *  view's cache-buster — changes, because a re-opened stream is another viewer. */
export function useViewerId(streamKey: number | string): string {
  const [held, setHeld] = useState(() => ({ streamKey, id: newViewerId() }));
  if (held.streamKey === streamKey) return held.id;
  const next = { streamKey, id: newViewerId() };
  setHeld(next);
  return next.id;
}

/** A read the rule acts on: the upstream is gone or silent, or it is fine and
 *  this view is no longer on it. */
export function isBadStreamRead(health: CameraStreamHealth): boolean {
  return !health.active || health.stalled || health.viewer_attached === false;
}

export type CameraStreamPhase = 'live' | 'waiting' | 'failed';

interface Options {
  /** The view is showing — or wants to show — a live stream. */
  enabled: boolean;
  /** The server's status for this source, asked with the id of the stream
   *  now open. `null` (and a rejected read) is "no answer": it neither clears
   *  nor adds a strike. Omitted where the view may not ask — then only the
   *  image's own `error` starts a recovery. */
  readHealth?: (viewerId: string) => Promise<CameraStreamHealth | null>;
  /** A view nobody can press Retry on (a broadcast overlay) keeps trying this
   *  often after it has given up. */
  slowRetryMs?: number;
}

export const HEALTH_CHECK_MS = 5000;
/** One bad read is a blip while the shared upstream starts or changes hands
 *  (upstream #2521); two in a row is a dead stream. */
export const STREAM_STRIKES = 2;

/** The count of bad reads in a row after this one. Every camera view — the
 *  wall's tile and the overlay through the hook below, the floating window and
 *  the camera page with their own reconnect UI — decides by this one rule:
 *  the stream is lost when the count reaches `STREAM_STRIKES`. */
export function strikesAfter(strikes: number, bad: boolean): number {
  return bad ? strikes + 1 : 0;
}
const MAX_ATTEMPTS = 5;
/** How long a stream must have been open, with a frame drawn, before a healthy
 *  answer gives it its retry budget back. Past the server's own stall window
 *  (10 s without a frame, after a 5 s start): a camera that sends one frame per
 *  connection and goes silent reads healthy until then, on every attempt. */
export const STEADY_AFTER_MS = 20_000;
const FIRST_DELAY_MS = 2000;
const MAX_DELAY_MS = 30_000;

/**
 * The one liveness rule of a view that draws an MJPEG stream and has no
 * controls of its own: the Cam Wall's live tile and the stream overlay.
 *
 * A stream the server ends cleanly fires nothing on the `<img>` — the browser
 * keeps the last frame. So the view asks the SERVER every few seconds, and
 * what it can detect is exactly what that answer says:
 *
 * - the source's shared upstream is gone or silent (`active` / `stalled`: the
 *   camera worker's budget ran out, the camera stopped sending) — true for
 *   every viewer of that camera at once;
 * - THIS view's own stream is no longer attached (`viewer_attached`, asked by
 *   the `viewerId` the view put on its stream request): its authority was
 *   retired or its token expired while somebody else keeps the upstream alive.
 *   A server that does not send the field leaves this case undetected — it is
 *   read as "cannot say", never as a dead stream.
 *
 * Two bad answers in a row, or an `error` on the image, take the image down
 * (`phase` leaves `live`, the view unmounts it and `useCameraImageRef` clears
 * its `src`, which aborts the request) and re-open it under a new `generation`
 * — and a new `viewerId` — after a growing wait: 2 s, 4 s, … 30 s. After five
 * attempts in a row it gives up (`failed`) until `restart`. "In a row" ends
 * when the SERVER has called the stream healthy `STEADY_AFTER_MS` after it
 * opened and drew a frame — not at the first frame, which a camera that then
 * goes silent delivers on every connection. (A view that may not ask the
 * server has only the frame to go by.)
 *
 * There is never a second stream beside a live one: a re-open is scheduled
 * only from `live`, and the old image is gone for the whole wait.
 *
 * A hidden page (a background tab, an OBS source that is not shown) neither
 * asks nor re-opens: a re-open that falls due waits, and happens once when
 * the page is shown again. A stream that is still running is left alone.
 */
export function useCameraStreamRecovery({ enabled, readHealth, slowRetryMs }: Options) {
  const [phase, setPhase] = useState<CameraStreamPhase>('live');
  const [generation, setGeneration] = useState(0);
  const viewerId = useViewerId(generation);
  const phaseRef = useRef<CameraStreamPhase>('live');
  const attemptsRef = useRef(0);
  const strikesRef = useRef(0);
  // The stream now open has drawn a frame; and when it was opened.
  const loadedRef = useRef(false);
  const openedAtRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dueRef = useRef(false);
  const readRef = useRef(readHealth);
  const slowRetryRef = useRef(slowRetryMs);
  useEffect(() => {
    readRef.current = readHealth;
    slowRetryRef.current = slowRetryMs;
  }, [readHealth, slowRetryMs]);

  const go = useCallback((next: CameraStreamPhase) => {
    phaseRef.current = next;
    setPhase(next);
  }, []);

  const clearPending = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = null;
    dueRef.current = false;
  }, []);

  const reopen = useCallback(() => {
    attemptsRef.current += 1;
    strikesRef.current = 0;
    setGeneration((value) => value + 1);
    go('live');
  }, [go]);

  const reopenAfter = useCallback((delay: number) => {
    clearPending();
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      if (document.hidden) dueRef.current = true;
      else reopen();
    }, delay);
  }, [clearPending, reopen]);

  /** The stream is gone: take the image down and plan the next attempt. */
  const lost = useCallback(() => {
    if (phaseRef.current !== 'live') return;
    if (attemptsRef.current >= MAX_ATTEMPTS) {
      go('failed');
      if (slowRetryRef.current) reopenAfter(slowRetryRef.current);
      return;
    }
    go('waiting');
    reopenAfter(Math.min(MAX_DELAY_MS, FIRST_DELAY_MS * 2 ** attemptsRef.current));
  }, [go, reopenAfter]);

  /** A frame arrived. Where the server can be asked, the budget waits for its
   *  word on a stream that kept delivering; elsewhere the frame is all there is. */
  const onLoad = useCallback(() => {
    loadedRef.current = true;
    strikesRef.current = 0;
    if (!readRef.current) attemptsRef.current = 0;
  }, []);

  /** Open the stream again now, with a fresh budget (Retry; a new selection). */
  const restart = useCallback(() => {
    clearPending();
    attemptsRef.current = 0;
    strikesRef.current = 0;
    setGeneration((value) => value + 1);
    go('live');
  }, [clearPending, go]);

  useEffect(() => {
    if (enabled) return;
    clearPending();
    attemptsRef.current = 0;
    strikesRef.current = 0;
    go('live');
  }, [enabled, clearPending, go]);

  useEffect(() => clearPending, [clearPending]);

  useEffect(() => {
    const shown = () => {
      if (document.hidden || !dueRef.current) return;
      dueRef.current = false;
      reopen();
    };
    document.addEventListener('visibilitychange', shown);
    return () => document.removeEventListener('visibilitychange', shown);
  }, [reopen]);

  useEffect(() => {
    if (!enabled || phase !== 'live') return;
    let cancelled = false;
    strikesRef.current = 0;
    loadedRef.current = false;
    openedAtRef.current = Date.now();
    const timer = setInterval(async () => {
      const read = readRef.current;
      if (!read || document.hidden) return;
      let health: CameraStreamHealth | null;
      try {
        health = await read(viewerId);
      } catch {
        return; // the server may be away for a moment; that is not the camera's answer
      }
      if (cancelled || health === null) return;
      strikesRef.current = strikesAfter(strikesRef.current, isBadStreamRead(health));
      if (strikesRef.current >= STREAM_STRIKES) lost();
      else if (strikesRef.current === 0 && loadedRef.current && Date.now() - openedAtRef.current >= STEADY_AFTER_MS) {
        attemptsRef.current = 0;
      }
    }, HEALTH_CHECK_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [enabled, phase, generation, viewerId, lost]);

  return { phase, generation, viewerId, onError: lost, onLoad, restart };
}
