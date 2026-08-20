import { useEffect, useState } from 'react';

/**
 * Tailwind's `lg`. ⚠️ Kept in sync with the `lg:` classes it is paired with —
 * components using this hook also switch layout via `lg:` utilities, and the
 * two disagreeing produces a half-applied layout.
 */
const WIDE_LAYOUT_BREAKPOINT = 1024;

/**
 * True when there is room for a side-by-side layout.
 *
 * ⚠️ Prefer plain `lg:` classes where CSS alone can do the job. This exists
 * for the cases where the BEHAVIOUR differs rather than only the styling — a
 * disclosure that collapses on narrow screens but is permanently open, and
 * inert, when it has its own column.
 */
export function useIsWideLayout(): boolean {
  const [isWide, setIsWide] = useState(() =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(`(min-width: ${WIDE_LAYOUT_BREAKPOINT}px)`).matches
      : false,
  );

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia(`(min-width: ${WIDE_LAYOUT_BREAKPOINT}px)`);
    const onChange = (event: MediaQueryListEvent) => setIsWide(event.matches);
    setIsWide(query.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  return isWide;
}
