/**
 * Real fullscreen (the Fullscreen API) for one element.
 *
 * `supported` is false where the browser has no element fullscreen (iPhone
 * Safari; Safari before 16.4 has only a prefixed one) — the caller then renders
 * no button at all, rather than one that does nothing.
 *
 * ⚠️ What is on screen in fullscreen is the element's own subtree and nothing
 * else: a dialog or a toast portalled into `body` is invisible there. Give it
 * an element that opens no dialog, and put the toggle INSIDE that element — it
 * is the only way out besides Escape.
 *
 * ⚠️ Escape belongs to the fullscreen first. The browser leaves fullscreen on
 * Escape by itself, and the modal stack closes the topmost modal on the same
 * key; whether the page sees that keydown, and whether before or after the
 * fullscreen is gone, differs by browser. So the state is read off
 * `fullscreenchange`, and a capture-phase listener — ahead of the modal
 * stack's — swallows an Escape that arrives while the element is fullscreen or
 * within `ESC_GRACE_MS` of its leaving. The next Escape is the modal's again.
 */
import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';

/** How long after fullscreen ended an Escape is still taken as the one that ended it. */
export const ESC_GRACE_MS = 400;

export function fullscreenSupported(): boolean {
  return (
    typeof document !== 'undefined' &&
    document.fullscreenEnabled === true &&
    typeof Element !== 'undefined' &&
    typeof Element.prototype.requestFullscreen === 'function'
  );
}

export interface ElementFullscreen {
  supported: boolean;
  /** The element is the document's fullscreen element right now. */
  active: boolean;
  toggle: () => void;
}

/**
 * @param target the element to show fullscreen
 * @param returnFocus where the focus goes when leaving fullscreen dropped it (the toggle)
 */
export function useElementFullscreen(
  target: RefObject<HTMLElement | null>,
  returnFocus?: RefObject<HTMLElement | null>,
): ElementFullscreen {
  const [supported] = useState(fullscreenSupported);
  const [active, setActive] = useState(false);
  const activeRef = useRef(false);
  const leftAt = useRef(Number.NEGATIVE_INFINITY);

  useEffect(() => {
    if (!supported) return;

    const isOurs = () => document.fullscreenElement != null && document.fullscreenElement === target.current;

    const onChange = () => {
      const now = isOurs();
      if (activeRef.current && !now) {
        leftAt.current = performance.now();
        // Leaving must not strand the keyboard on <body>, outside the dialog.
        const focused = document.activeElement;
        if (!focused || focused === document.body) returnFocus?.current?.focus();
      }
      activeRef.current = now;
      setActive(now);
    };

    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      const inFullscreen = isOurs();
      if (!inFullscreen && performance.now() - leftAt.current >= ESC_GRACE_MS) return;
      e.stopImmediatePropagation();
      e.preventDefault();
      if (inFullscreen) void document.exitFullscreen().catch(() => undefined);
    };

    document.addEventListener('fullscreenchange', onChange);
    window.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('fullscreenchange', onChange);
      window.removeEventListener('keydown', onKey, true);
    };
  }, [supported, target, returnFocus]);

  const toggle = useCallback(() => {
    const el = target.current;
    if (!el || !supported) return;
    if (document.fullscreenElement === el) void document.exitFullscreen().catch(() => undefined);
    else void el.requestFullscreen().catch(() => undefined);
  }, [supported, target]);

  return { supported, active, toggle };
}
