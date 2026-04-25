import { createContext, useContext, useMemo, useState, useEffect, type ReactNode } from 'react';

interface ConnectionContextValue {
  /** True while the WebSocket is open. False during reconnect or after a tab
   *  was backgrounded long enough for the browser to suspend the socket. */
  isConnected: boolean;
  /** True iff the WS has been disconnected for at least 2 s. The short delay
   *  prevents the indicator from flashing for sub-second blips during normal
   *  reconnects. */
  showOfflineIndicator: boolean;
  /** Setter exposed to the WS hook. Don't call from components. */
  setIsConnected: (connected: boolean) => void;
}

const ConnectionContext = createContext<ConnectionContextValue | null>(null);

const OFFLINE_GRACE_MS = 2000;

export function ConnectionProvider({ children }: { children: ReactNode }) {
  const [isConnected, setIsConnected] = useState(true);
  const [showOfflineIndicator, setShowOfflineIndicator] = useState(false);

  useEffect(() => {
    if (isConnected) {
      setShowOfflineIndicator(false);
      return;
    }
    const timer = window.setTimeout(() => setShowOfflineIndicator(true), OFFLINE_GRACE_MS);
    return () => clearTimeout(timer);
  }, [isConnected]);

  const value = useMemo<ConnectionContextValue>(
    () => ({ isConnected, showOfflineIndicator, setIsConnected }),
    [isConnected, showOfflineIndicator],
  );

  return <ConnectionContext.Provider value={value}>{children}</ConnectionContext.Provider>;
}

export function useConnection(): ConnectionContextValue {
  const ctx = useContext(ConnectionContext);
  if (!ctx) {
    // Defensive default — components rendered outside the provider (e.g. during
    // tests) should still mount cleanly. Treat as "connected" so they don't
    // render warning indicators in environments that don't have the WS layer.
    return {
      isConnected: true,
      showOfflineIndicator: false,
      setIsConnected: () => {},
    };
  }
  return ctx;
}
