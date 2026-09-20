import { useEffect, useLayoutEffect, useRef } from 'react';

/**
 * Move focus INTO an overlay when it opens, and give it back when it closes.
 *
 * The pattern is the product gallery's lightbox, lifted out of it so that every
 * overlay this app opens over a page answers a keyboard the same way. Attach
 * the returned ref to the element that carries `role="dialog"`, give that
 * element `tabIndex={-1}` so it can hold focus, and pass whether it is open.
 *
 * Since the modal shell (`components/Modal.tsx`) landed, every modal and
 * lightbox goes through it, so the shell is the caller — grep this file's name
 * rather than trusting any list written here; a list is what went stale last
 * time.
 *
 * ⚠️ **This hook is the two ENDS of the trap, not the trap.** The trap itself
 * is `inert`: while a modal is open the modal stack (`components/modalStack.ts`)
 * marks `#root` and every non-top modal inert, so nothing in the page behind
 * can take focus and Tab has nowhere to go (the toast viewport and popovers
 * portalled into `body` stay live on purpose). What this hook fixes is
 * where focus starts and where it ends up. Without it a keyboard user who
 * opened the overlay would start at the top of the document, and on close the
 * focus ring would be left on `<body>`, which is nowhere.
 *
 * ⚠️ The return happens in an effect cleanup, and `focus()` on an element
 * inside an inert subtree is a silent no-op. The shell therefore calls
 * `useModalStackEntry` BEFORE this hook — React runs cleanups in declaration
 * order, so the layer below is live again by the time the opener is focused.
 * `Modal.test.tsx` pins that order; do not reorder the hooks in the shell.
 *
 * ⚠️ The opener is READ in a layout effect and focus is RETURNED in the
 * passive cleanup — on purpose, and they must not be merged: the read has
 * to happen before the stack's passive effect marks `#root` inert, the
 * return has to happen after the stack's passive cleanup makes it live.
 * When the opener is still inside an inert subtree at cleanup time — two
 * modals unmounting in one commit, cleaned up top-down — the return is
 * deferred by a microtask, which runs after the whole passive flush.
 *
 * ⚠️ The element to return focus TO is read at OPEN, not at close: by the time
 * the overlay unmounts `document.activeElement` is whatever the overlay left
 * focused, i.e. the overlay itself.
 */
export function useDialogFocus<T extends HTMLElement>(open: boolean) {
  const ref = useRef<T | null>(null);
  const returnFocusTo = useRef<HTMLElement | null>(null);

  // The opener is read in a LAYOUT effect: layout effects run before every
  // passive effect of the commit, so this happens before the modal stack
  // (a passive effect in useModalStackEntry) marks #root inert. The HTML
  // focus-fixup rule for an ancestor turning inert runs at "update the
  // rendering" (whatwg/html#8392), so a passive read would still see the
  // opener today — the layout read simply does not depend on that timing.
  useLayoutEffect(() => {
    if (!open) return;
    returnFocusTo.current = document.activeElement as HTMLElement | null;
  }, [open]);

  useEffect(() => {
    if (!open) return;
    ref.current?.focus();
    return () => {
      const el = returnFocusTo.current;
      returnFocusTo.current = null;
      // `?.` on the method as well as on the ref: jsdom hands back elements
      // that have been detached from the document, and a page that navigated
      // away has nothing left to focus.
      if (el?.closest?.('[inert]')) {
        // Still inside an inert subtree: two modals are unmounting in ONE
        // commit and React ran this outer cleanup before the inner modal
        // unregistered (deleted subtrees clean up top-down). A microtask runs
        // after the whole passive flush — every unregister done, #root live —
        // and still before the browser's focus fixup at "update the rendering".
        queueMicrotask(() => el.focus?.());
      } else {
        el?.focus?.();
      }
    };
  }, [open]);

  return ref;
}
