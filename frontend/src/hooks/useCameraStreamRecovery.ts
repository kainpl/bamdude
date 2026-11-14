import { useCallback, useEffect, useRef, useState } from 'react';

/** What the server says about one source's stream. */
export interface CameraStreamHealth {
  active: boolean;
  stalled: boolean;
}

export type CameraStreamPhase = 'live' | 'waiting' | 'failed';

interface Options {
  /** The view is showing — or wants to show — a live stream. */
  enabled: boolean;
  /** The server's status for this source. `null` (and a rejected read) is "no
   *  answer": it neither clears nor adds a strike. Omitted where the view may
   *  not ask — then only the image's own `error` starts a recovery. */
  readHealth?: () => Promise<CameraStreamHealth | null>;
  /** A view nobody can press Retry on (a broadcast overlay) keeps trying this
   *  often after it has given up. */
  slowRetryMs?: number;
}

export const HEALTH_CHECK_MS = 5000;
/** One bad read is a blip while the shared upstream starts or changes hands
 *  (upstream #2521); two in a row is a dead stream. */
const STRIKES = 2;
const MAX_ATTEMPTS = 5;
const FIRST_DELAY_MS = 2000;
const MAX_DELAY_MS = 30_000;

/**
 * The one liveness rule of a view that draws an MJPEG stream and has no
 * controls of its own: the Cam Wall's live tile and the stream overlay.
 *
 * A stream the server ends cleanly (the camera worker's budget ran out, the
 * viewer's authority was retired, a token expired) fires nothing on the
 * `<img>` — the browser keeps the last frame. So the view asks the SERVER
 * about its source every few seconds; two bad answers in a row, or an `error`
 * on the image, take the image down (`phase` leaves `live`, the view unmounts
 * it and `useCameraImageRef` clears its `src`, which aborts the request) and
 * re-open it under a new `generation` after a growing wait: 2 s, 4 s, … 30 s.
 * After five attempts without a frame it gives up (`failed`) until `restart`.
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
  const phaseRef = useRef<CameraStreamPhase>('live');
  const attemptsRef = useRef(0);
  const strikesRef = useRef(0);
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

  /** A frame arrived: the next loss starts from the short wait again. */
  const onLoad = useCallback(() => {
    attemptsRef.current = 0;
    strikesRef.current = 0;
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
    const timer = setInterval(async () => {
      const read = readRef.current;
      if (!read || document.hidden) return;
      let health: CameraStreamHealth | null;
      try {
        health = await read();
      } catch {
        return; // the server may be away for a moment; that is not the camera's answer
      }
      if (cancelled || health === null) return;
      if (health.active && !health.stalled) {
        strikesRef.current = 0;
        return;
      }
      strikesRef.current += 1;
      if (strikesRef.current >= STRIKES) lost();
    }, HEALTH_CHECK_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [enabled, phase, generation, lost]);

  return { phase, generation, onError: lost, onLoad, restart };
}
