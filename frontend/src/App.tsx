import React, { useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from './api/client';
import { Layout } from './components/Layout';
import { PrintersPage } from './pages/PrintersPage';
import { ArchivesPage } from './pages/ArchivesPage';
import { QueuePage } from './pages/QueuePage';
import { StatsPage } from './pages/StatsPage';
import { SettingsPage } from './pages/SettingsPage';
import { ProfilesPage } from './pages/ProfilesPage';
import { MaintenancePage } from './pages/MaintenancePage';
import { ProjectsPage } from './pages/ProjectsPage';
import { ProjectDetailPage } from './pages/ProjectDetailPage';
import { FileManagerPage } from './pages/FileManagerPage';
import { LibraryTrashPage } from './pages/LibraryTrashPage';
import { ArchiveTrashPage } from './pages/ArchiveTrashPage';
import { MakerworldPage } from './pages/MakerworldPage';
import { CameraPage } from './pages/CameraPage';
import { StreamOverlayPage } from './pages/StreamOverlayPage';
import { ExternalLinkPage } from './pages/ExternalLinkPage';
import { GroupEditPage } from './pages/GroupEditPage';
import InventoryPage from './pages/InventoryPage';
import { SystemInfoPage } from './pages/SystemInfoPage';
import { LoginPage } from './pages/LoginPage';
import { SetupPage } from './pages/SetupPage';
import { NotificationsPage } from './pages/NotificationsPage';
import { GCodeViewerPage } from './pages/GCodeViewerPage';
import { useWebSocket } from './hooks/useWebSocket';
import { useStreamTokenSync } from './hooks/useCameraStreamToken';
import { ThemeProvider } from './contexts/ThemeContext';
import { ToastProvider } from './contexts/ToastContext';
import { SliceJobTrackerProvider } from './contexts/SliceJobTrackerContext';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import { ColorCatalogProvider } from './contexts/ColorCatalogContext';
import { ConnectionProvider } from './contexts/ConnectionContext';
import { useConnectionToast } from './hooks/useConnectionToast';
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000 * 60,
      retry: 1,
    },
  },
});

function WebSocketProvider({ children }: { children: React.ReactNode }) {
  useWebSocket();
  useConnectionToast();
  return <>{children}</>;
}

function StreamTokenSync() {
  useStreamTokenSync();
  return null;
}

/**
 * Pulls the authoritative system language from the server on first mount
 * after auth and forces i18n to match.  Server-side `settings.language`
 * is the source of truth because it also drives backend outputs
 * (notification templates, maintenance-type names, Telegram bot),
 * and the farm operator configures it once for the whole install —
 * individual browsers shouldn't override that via auto-detection.
 *
 * User-driven picks in Settings UI still write to the server (that's
 * the intentional override path); this effect only corrects cases
 * where the browser auto-detected a different language than configured.
 */
function LanguageSync({ children }: { children: React.ReactNode }) {
  const { i18n } = useTranslation();
  const syncedRef = React.useRef(false);

  useEffect(() => {
    if (syncedRef.current) return;
    syncedRef.current = true;
    (async () => {
      try {
        const settings = await api.getSettings();
        const serverLang = settings.language;
        if (serverLang && serverLang !== i18n.language) {
          await i18n.changeLanguage(serverLang);
        }
      } catch {
        // Best-effort — if /settings fails we leave i18n on its detected value.
      }
    })();
  }, [i18n]);

  return <>{children}</>;
}

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { loading, user, requiresSetup } = useAuth();

  if (loading) {
    return <div className="min-h-screen flex items-center justify-center">Loading...</div>;
  }

  // First-boot gate: no admin user yet → the backend's setup middleware 503s
  // everything but /auth/status + /auth/setup. Route the user at /setup so
  // they can create the initial admin instead of bouncing to /login where
  // the form would just fail with "setup required".
  if (requiresSetup) {
    return <Navigate to="/setup" replace />;
  }

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}

function PermissionRoute({ permission, children }: { permission: string; children: React.ReactNode }) {
  // Permission-gated route: any user holding the given permission can enter,
  // not just admins. Individual components below this guard apply their own
  // per-action permission checks (e.g. SettingsPage tabs each consult their
  // own write permission). Used for pages where delegation is supported —
  // settings:read grants read-only Settings, groups:create lets a delegated
  // user open the new-group form, etc.
  const { loading, user, hasPermission, requiresSetup } = useAuth();

  if (loading) {
    return <div className="min-h-screen flex items-center justify-center">Loading...</div>;
  }

  if (requiresSetup) {
    return <Navigate to="/setup" replace />;
  }

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  if (!hasPermission(permission as Parameters<typeof hasPermission>[0])) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}

function SetupRoute({ children }: { children: React.ReactNode }) {
  const { loading, requiresSetup } = useAuth();

  if (loading) {
    return <div className="min-h-screen flex items-center justify-center">Loading...</div>;
  }

  // Setup is a ONE-TIME bootstrap flow: allow /setup only while the backend
  // still reports ``requires_setup=true``. Once the initial admin exists, a
  // navigation to /setup bounces to /login (returning users can't accidentally
  // re-enter the setup form). Old behaviour keyed off the now-removed
  // ``authEnabled`` opt-in flag (always ``true`` post-0.4.0), which meant
  // fresh installs were immediately redirected away from /setup — breaking
  // first boot.
  if (!requiresSetup) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}

class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean; error: Error | null }
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen flex items-center justify-center bg-bambu-dark p-8">
          <div className="max-w-lg w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl p-6 text-center">
            <h1 className="text-xl font-bold text-white mb-2">Something went wrong</h1>
            <p className="text-bambu-gray text-sm mb-4">{this.state.error?.message}</p>
            <details className="text-left mb-4">
              <summary className="text-xs text-bambu-gray cursor-pointer">Stack trace</summary>
              <pre className="mt-2 text-xs text-red-400 overflow-auto max-h-48 p-2 bg-bambu-dark rounded">
                {this.state.error?.stack}
              </pre>
            </details>
            <button
              className="px-4 py-2 bg-bambu-green hover:bg-bambu-green-light text-white rounded-lg text-sm"
              onClick={() => {
                this.setState({ hasError: false, error: null });
                window.location.href = '/';
              }}
            >
              Reload
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

function App() {
  return (
    <ErrorBoundary>
    <ThemeProvider>
      <ToastProvider>
        <QueryClientProvider client={queryClient}>
          <AuthProvider>
            <ConnectionProvider>
            <ColorCatalogProvider>
            <SliceJobTrackerProvider>
            <StreamTokenSync />
            <BrowserRouter>
              <Routes>
                {/* Setup page - only accessible if auth not enabled */}
                <Route path="/setup" element={<SetupRoute><SetupPage /></SetupRoute>} />

                {/* Login page */}
                <Route path="/login" element={<LoginPage />} />

                {/* Camera page - standalone, no layout, no WebSocket (doesn't need real-time updates) */}
                <Route path="/camera/:printerId" element={<CameraPage />} />

                {/* Stream overlay page - standalone for OBS/streaming embeds, no auth required */}
                <Route path="/overlay/:printerId" element={<StreamOverlayPage />} />

                {/* Main app with WebSocket for real-time updates */}
                <Route element={<ProtectedRoute><LanguageSync><WebSocketProvider><Layout /></WebSocketProvider></LanguageSync></ProtectedRoute>}>
                  <Route index element={<PrintersPage />} />
                  <Route path="archives" element={<ArchivesPage />} />
                  <Route path="archives/trash" element={<ArchiveTrashPage />} />
                  <Route path="queue" element={<QueuePage />} />
                  <Route path="stats" element={<StatsPage />} />
                  <Route path="profiles" element={<ProfilesPage />} />
                  <Route path="maintenance" element={<MaintenancePage />} />
                  <Route path="projects" element={<ProjectsPage />} />
                  <Route path="projects/:id" element={<ProjectDetailPage />} />
                  <Route path="inventory" element={<InventoryPage />} />
                  <Route path="files" element={<FileManagerPage />} />
                  <Route path="files/trash" element={<LibraryTrashPage />} />
                  <Route path="makerworld" element={<PermissionRoute permission="makerworld:view"><MakerworldPage /></PermissionRoute>} />
                  <Route path="settings" element={<PermissionRoute permission="settings:read"><SettingsPage /></PermissionRoute>} />
                  <Route path="groups/new" element={<PermissionRoute permission="groups:create"><GroupEditPage /></PermissionRoute>} />
                  <Route path="groups/:id/edit" element={<PermissionRoute permission="groups:update"><GroupEditPage /></PermissionRoute>} />
                  <Route path="users" element={<Navigate to="/settings?tab=users" replace />} />
                  <Route path="groups" element={<Navigate to="/settings?tab=users" replace />} />
                  <Route path="system" element={<SystemInfoPage />} />
                  <Route path="notifications" element={<NotificationsPage />} />
                  <Route path="gcode-viewer" element={<GCodeViewerPage />} />
                  <Route path="external/:id" element={<ExternalLinkPage />} />
                </Route>
              </Routes>
            </BrowserRouter>
            </SliceJobTrackerProvider>
            </ColorCatalogProvider>
            </ConnectionProvider>
          </AuthProvider>
        </QueryClientProvider>
      </ToastProvider>
    </ThemeProvider>
    </ErrorBoundary>
  );
}

export default App;
