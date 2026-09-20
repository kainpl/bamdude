import { useEffect, useState } from 'react';

function readTarget(): number | null {
  const value = Number(new URLSearchParams(window.location.search).get('monitorPrinter'));
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

/** A temporary deep-link filter; it never changes the operator's saved filters. */
export function useMonitorTarget() {
  const [target, setTarget] = useState(readTarget);
  useEffect(() => {
    const read = () => setTarget(readTarget());
    window.addEventListener('popstate', read);
    return () => window.removeEventListener('popstate', read);
  }, []);
  const clear = () => {
    const url = new URL(window.location.href);
    url.searchParams.delete('monitorPrinter');
    window.history.replaceState(window.history.state, '', url);
    window.dispatchEvent(new PopStateEvent('popstate', { state: window.history.state }));
    setTarget(null);
  };
  return [target, clear] as const;
}
