/**
 * Pairing progress: a local countdown plus whatever the coordinator reports.
 *
 * Separate from the card for two reasons. It is testable without rendering, and
 * the countdown is genuinely local state — nothing in the backend reports the
 * join window closing, so the UI has to track the window it asked for.
 *
 * Events arrive as window events dispatched by `useWebSocket`, the same pattern
 * the existing `background_dispatch` case uses. Query invalidation alone would
 * not do: the card needs the individual event ("this device was rejected, and
 * here is why"), not a refetch.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export type PairingEvent = {
  kind: 'joining' | 'paired' | 'rejected';
  ieee: string;
  model: string | null;
};

type Phase = 'idle' | 'pairing';

const EVENT_KINDS: Array<[string, PairingEvent['kind']]> = [
  ['zigbee-device-joining', 'joining'],
  ['zigbee-device-paired', 'paired'],
  ['zigbee-device-rejected', 'rejected'],
];

/** Pull ieee/model out of either message shape.
 *
 * `joining` carries a bare `ieee` (the device has not been interviewed yet, so
 * there is no model to report); `paired` and `rejected` carry a whole described
 * device. Normalising here keeps that asymmetry out of the card.
 */
function toEvent(kind: PairingEvent['kind'], detail: unknown): PairingEvent | null {
  const payload = (detail ?? {}) as { ieee?: string; device?: { ieee?: string; model?: string | null } };
  const ieee = payload.device?.ieee ?? payload.ieee;
  if (!ieee) return null;
  return { kind, ieee, model: payload.device?.model ?? null };
}

export function usePairingProgress() {
  const [phase, setPhase] = useState<Phase>('idle');
  const [secondsLeft, setSecondsLeft] = useState(0);
  const [events, setEvents] = useState<PairingEvent[]>([]);

  // The listeners are registered once and gated on the phase through a ref, so
  // starting a window does not tear down and rebuild three subscriptions.
  const pairingRef = useRef(false);
  pairingRef.current = phase === 'pairing';

  const start = useCallback((seconds: number) => {
    // Clear the previous run: last session's rejection sitting under this
    // session's countdown reads as something that just happened.
    setEvents([]);
    setSecondsLeft(seconds);
    setPhase('pairing');
  }, []);

  useEffect(() => {
    if (phase !== 'pairing') return;
    const timer = window.setInterval(() => {
      setSecondsLeft((left) => {
        if (left <= 1) {
          setPhase('idle');
          return 0;
        }
        return left - 1;
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [phase]);

  useEffect(() => {
    const handlers = EVENT_KINDS.map(([name, kind]) => {
      const handler = (raw: Event) => {
        if (!pairingRef.current) return;
        const event = toEvent(kind, (raw as CustomEvent).detail);
        if (event) setEvents((previous) => [...previous, event]);
      };
      window.addEventListener(name, handler);
      return [name, handler] as const;
    });
    return () => handlers.forEach(([name, handler]) => window.removeEventListener(name, handler));
  }, []);

  return { phase, secondsLeft, events, start };
}
