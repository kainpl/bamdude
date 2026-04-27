import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { api, getAuthToken, setAuthToken } from '../api/client';
import type { Permission, UserResponse } from '../api/client';

interface AuthContextType {
  user: UserResponse | null;
  /**
   * Kept for backward compatibility with consumers that used to check whether
   * the deployment had opt-in auth. The opt-in mode has been removed; auth is
   * always on, so this is a permanent ``true``. New code should not read it.
   */
  authEnabled: true;
  requiresSetup: boolean;
  loading: boolean;
  isAdmin: boolean;
  login: (username: string, password: string, rememberMe?: boolean) => Promise<import('../api/client').LoginResponse>;
  loginWithToken: (token: string, user: UserResponse) => void;
  logout: () => void;
  refreshUser: () => Promise<void>;
  refreshAuth: () => Promise<void>;
  hasPermission: (permission: Permission) => boolean;
  hasAnyPermission: (...permissions: Permission[]) => boolean;
  hasAllPermissions: (...permissions: Permission[]) => boolean;
  canModify: (resource: 'queue' | 'archives' | 'library', action: 'update' | 'delete' | 'reprint', createdById: number | null | undefined) => boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<UserResponse | null>(null);
  const [requiresSetup, setRequiresSetup] = useState(false);
  const [loading, setLoading] = useState(true);
  const hasRedirectedRef = useRef(false);
  const mountedRef = useRef(true);

  const checkAuthStatus = async () => {
    try {
      // No `?token=` URL-param bootstrap on purpose — it's a session-fixation
      // vector (attacker-crafted /?token=… would overwrite localStorage with
      // the attacker's token before any server verify). If a future feature
      // ever needs cross-origin auth bootstrap, do it through a non-persistent
      // setAuthToken path that doesn't touch localStorage.
      const status = await api.getAuthStatus();
      if (!mountedRef.current) return;
      setRequiresSetup(status.requires_setup);

      // If setup is required, don't try to load a user - even a cached token
      // can't be validated against a system that has no admin yet.
      if (status.requires_setup) {
        setUser(null);
        return;
      }

      const token = getAuthToken();
      if (token) {
        try {
          const currentUser = await api.getCurrentUser();
          if (!mountedRef.current) return;
          setUser(currentUser);
        } catch {
          // Token invalid/expired - drop it and force re-login.
          setAuthToken(null);
          if (!mountedRef.current) return;
          setUser(null);
        }
      } else {
        setUser(null);
      }
    } catch {
      if (!mountedRef.current) return;
      setUser(null);
    } finally {
      if (mountedRef.current) {
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    // Check auth status on mount
    checkAuthStatus();
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Listen for server-side auth invalidation (token expired, revoked, etc.)
  // broadcast by api/client.ts request() when it gets a matching 401. Without
  // this the frontend would sit on stale React state showing a logged-in UI
  // even though localStorage was already cleared — background polls just
  // 401'd silently. Now we clear React state and hard-redirect to /login so
  // the operator notices immediately instead of on the next navigation.
  useEffect(() => {
    const handleInvalidated = () => {
      if (!mountedRef.current) return;
      setUser(null);
      const path = window.location.pathname;
      // /login handles its own flow; /setup is pre-admin bootstrap; standalone
      // /camera/:id and /overlay/:id routes carry their own short-lived camera
      // tokens and shouldn't bounce to login on a session-token expiry there.
      const exempt =
        path === '/login' ||
        path === '/setup' ||
        path.startsWith('/camera/') ||
        path.startsWith('/overlay/');
      if (!exempt) {
        // hard navigation drops all cached queries + in-flight fetches, so
        // the next render starts from a clean, unauthenticated state.
        window.location.href = '/login';
      }
    };
    window.addEventListener('bamdude:auth-invalidated', handleInvalidated);
    return () => window.removeEventListener('bamdude:auth-invalidated', handleInvalidated);
  }, []);

  // Revalidate when the tab regains focus after being hidden. Covers the
  // case where the user leaves BamDude open overnight, the JWT expires
  // silently (no API calls while hidden → no 401 event to hook onto), and
  // then they come back to find stale UI. Hitting /auth/me forces the
  // expired-token check; a 401 here flows through the same invalidation
  // path above. Gated on `user` so we don't ping /auth/me on every focus
  // for logged-out visitors on /login.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return;
      if (!mountedRef.current || !user) return;
      if (!getAuthToken()) return;
      api.getCurrentUser().catch(() => {
        // 401 path already dispatches bamdude:auth-invalidated via
        // request(); other errors (network blip) are safe to swallow
        // — next real user action will retry.
      });
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, [user]);

  // Redirect to /setup when the backend reports no admin exists yet.
  useEffect(() => {
    if (loading) return;
    if (requiresSetup) {
      const currentPath = window.location.pathname;
      if (currentPath !== '/setup' && !currentPath.startsWith('/camera/') && !hasRedirectedRef.current) {
        hasRedirectedRef.current = true;
        window.location.href = '/setup';
      }
    } else {
      hasRedirectedRef.current = false;
    }
  }, [loading, requiresSetup]);

  const login = async (username: string, password: string, rememberMe: boolean = false) => {
    const response = await api.login({ username, password, remember_me: rememberMe });
    // When the server signals 2FA is required, the caller (LoginPage step
    // machine, §18.11) takes over with the pre_auth_token — do NOT set a
    // bearer token in that branch. Only set the token + refresh the user
    // when the server returned a fully-authenticated session.
    if (response.access_token && response.user) {
      setAuthToken(response.access_token);
      await checkAuthStatus();
    }
    return response;
  };

  const loginWithToken = (token: string, nextUser: UserResponse) => {
    // Called by LoginPage after /auth/2fa/verify or /auth/oidc/exchange —
    // at that point the server has already issued a full JWT, so we just
    // persist it and hydrate the user immediately without another round
    // trip to /auth/me.
    setAuthToken(token);
    if (mountedRef.current) {
      setUser(nextUser);
      setRequiresSetup(false);
    }
  };

  const logout = () => {
    setAuthToken(null);
    setUser(null);
    api.logout().catch(() => {
      // Ignore logout errors
    });
    window.location.href = '/login';
  };

  const refreshUser = async () => {
    if (getAuthToken()) {
      try {
        const currentUser = await api.getCurrentUser();
        if (mountedRef.current) {
          setUser(currentUser);
        }
      } catch {
        setAuthToken(null);
        if (mountedRef.current) {
          setUser(null);
        }
      }
    }
  };

  const refreshAuth = async () => {
    await checkAuthStatus();
  };

  // Memoize permission set for efficient lookups
  const permissionSet = useMemo(() => {
    return new Set(user?.permissions ?? []);
  }, [user?.permissions]);

  // Computed admin status
  const isAdmin = useMemo(() => user?.is_admin ?? false, [user?.is_admin]);

  // Permission check functions
  const hasPermission = useCallback((permission: Permission): boolean => {
    if (isAdmin) return true; // Admins have all permissions
    return permissionSet.has(permission);
  }, [isAdmin, permissionSet]);

  const hasAnyPermission = useCallback((...permissions: Permission[]): boolean => {
    if (isAdmin) return true;
    return permissions.some(p => permissionSet.has(p));
  }, [isAdmin, permissionSet]);

  const hasAllPermissions = useCallback((...permissions: Permission[]): boolean => {
    if (isAdmin) return true;
    return permissions.every(p => permissionSet.has(p));
  }, [isAdmin, permissionSet]);

  // Ownership-based permission check
  const canModify = useCallback((
    resource: 'queue' | 'archives' | 'library',
    action: 'update' | 'delete' | 'reprint',
    createdById: number | null | undefined,
  ): boolean => {
    if (isAdmin) return true;  // Admins can modify anything

    const allPerm = `${resource}:${action}_all` as Permission;
    const ownPerm = `${resource}:${action}_own` as Permission;

    // User has *_all permission - can modify any item
    if (permissionSet.has(allPerm)) return true;

    // User has *_own permission - can only modify their own items
    if (permissionSet.has(ownPerm)) {
      // Ownerless items (null created_by_id) require *_all permission
      if (createdById == null) return false;
      return createdById === user?.id;
    }

    return false;
  }, [isAdmin, permissionSet, user?.id]);

  return (
    <AuthContext.Provider
      value={{
        user,
        authEnabled: true,
        requiresSetup,
        loading,
        isAdmin,
        login,
        loginWithToken,
        logout,
        refreshUser,
        refreshAuth,
        hasPermission,
        hasAnyPermission,
        hasAllPermissions,
        canModify,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
