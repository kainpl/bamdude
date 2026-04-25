import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useConnection } from '../contexts/ConnectionContext';
import { useToast } from '../contexts/ToastContext';

const RECONNECT_TOAST_ID = 'ws-reconnecting';
const OFFLINE_GRACE_MS = 2000;

/**
 * Surfaces WebSocket connection drops via the standard toast system.
 *
 * - On disconnect → after 2 s grace, show a persistent ``warning`` toast
 *   ("Reconnecting…"). The grace prevents flicker during sub-second
 *   reconnects, which are normal whenever the visibility-sync forces a
 *   close+reopen.
 * - On reconnect → immediately dismiss the toast.
 *
 * Reuses ``ToastProvider`` so the styling matches every other notification
 * in the app (macro-executed, plate-not-empty, dispatch progress, etc.) —
 * no separate banner component to keep visually consistent.
 */
export function useConnectionToast() {
  const { isConnected } = useConnection();
  const { showPersistentToast, dismissToast } = useToast();
  const { t } = useTranslation();
  const graceTimerRef = useRef<number | null>(null);

  useEffect(() => {
    // Connected → cancel any pending grace timer + tear down the toast.
    if (isConnected) {
      if (graceTimerRef.current !== null) {
        clearTimeout(graceTimerRef.current);
        graceTimerRef.current = null;
      }
      dismissToast(RECONNECT_TOAST_ID);
      return;
    }

    // Disconnected → arm grace timer (only if not already armed). When it
    // fires, show the toast. If we reconnect before then, the cleanup above
    // disarms it before the user sees anything.
    if (graceTimerRef.current === null) {
      graceTimerRef.current = window.setTimeout(() => {
        graceTimerRef.current = null;
        showPersistentToast(RECONNECT_TOAST_ID, t('common.reconnecting'), 'warning');
      }, OFFLINE_GRACE_MS);
    }

    return () => {
      if (graceTimerRef.current !== null) {
        clearTimeout(graceTimerRef.current);
        graceTimerRef.current = null;
      }
    };
  }, [isConnected, showPersistentToast, dismissToast, t]);
}
