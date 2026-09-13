import { useEffect } from 'react';
import { clearLiveStatusPriority, setLiveStatusPriority } from '../utils/liveStatusPriority';

/** Track the cards actually mounted by a page's normal or virtual grid. */
export function useMountedPrinterPriority(scope: string) {
  useEffect(() => {
    const refresh = () => {
      const ids = Array.from(document.querySelectorAll<HTMLElement>('[data-live-status-printer-id]'))
        .map(element => Number(element.dataset.liveStatusPrinterId))
        .filter(Number.isFinite);
      setLiveStatusPriority(scope, ids);
    };
    refresh();
    const observer = new MutationObserver(refresh);
    observer.observe(document.body, { childList: true, subtree: true });
    return () => {
      observer.disconnect();
      clearLiveStatusPriority(scope);
    };
  }, [scope]);
}
