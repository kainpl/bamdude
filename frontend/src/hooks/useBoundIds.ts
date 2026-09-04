import { useState } from 'react';

/**
 * The ids a picker ARRIVED bound to, frozen at mount.
 *
 * `selectableProducts` (and `selectableProjects`) keep a retired row on offer
 * only because something is bound to it. The set that decides that must not be
 * the LIVE selection: unticking a chip for an inactive product removed it from
 * the live set, which removed the chip itself, and the operator could not
 * change their mind without reopening the dialog. Freezing at mount is also
 * what makes the offer survive a refetch — a product somebody retires in
 * another tab while this form is open stays on screen instead of vanishing
 * mid-edit and taking the binding with it.
 *
 * ⚠️ **Frozen means frozen.** A parent that points `value` at a RETIRED id
 * after mount will not see it offered. No caller does that — a picker is
 * mounted with the binding it edits, and the only ids it can be moved to
 * afterwards are ones it offered in the first place (or a product just
 * created, which is active by definition).
 *
 * `useState` with an initialiser rather than `useRef(initial)`: the argument of
 * `useRef` is evaluated on every render and thrown away, which reads as if the
 * value could change and rebuilds a Set for nothing.
 */
export function useBoundIds(initial: Iterable<number> | number | null | undefined): ReadonlySet<number> {
  const [frozen] = useState<ReadonlySet<number>>(() => {
    if (initial == null) return new Set<number>();
    return new Set(typeof initial === 'number' ? [initial] : initial);
  });
  return frozen;
}
