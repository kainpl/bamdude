import { useCallback, useEffect, useRef, useState } from 'react';

/** How long the search box waits before it becomes a request. */
export const SEARCH_DEBOUNCE_MS = 300;

/**
 * The text in a list's search box, debounced into the URL's `q`.
 *
 * The box follows the URL when `q` changes from OUTSIDE (Back, a shared link,
 * the empty state's Reset) and never while the user types: `written` is the q
 * this box last put there, so an echo of our own write cannot overwrite a
 * keystroke typed during the debounce.
 */
export function useSearchBox(q: string, setQ: (value: string) => void) {
  const [typed, setTyped] = useState(q);
  const written = useRef(q);

  useEffect(() => {
    if (q !== written.current) {
      written.current = q;
      setTyped(q);
    }
  }, [q]);

  useEffect(() => {
    const timer = setTimeout(() => {
      const next = typed.trim();
      if (next !== written.current) {
        written.current = next;
        setQ(next);
      }
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [typed, setQ]);

  /** Empty the box AND forget the q, for a caller that clears the URL itself. */
  const forget = useCallback(() => {
    written.current = '';
    setTyped('');
  }, []);

  return { typed, setTyped, forget };
}
