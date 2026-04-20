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
  login: (username: string, password: string) => Promise<void>;
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
      // Note: no `?token=` URL-param bootstrap. That flow was introduced for the
      // SpoolBuddy kiosk launcher (removed in the BamDude fork) and was a session-
      // fixation vector — an attacker-crafted link like /?token=ATTACKER_TOKEN would
      // overwrite localStorage with the attacker's token before any server verify.
      // If a future feature needs cross-origin auth bootstrap, port upstream's
      // 2-arg setAuthToken(token, persistToLocalStorage=false) from PR #933 instead.

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

  const login = async (username: string, password: string) => {
    const response = await api.login({ username, password });
    // LoginResponse.access_token is optional (undefined when 2FA is required).
    // Batch G (§18.11) adds the step-machine — until then we treat a missing
    // token the same as a failed login so callers aren't silently logged in.
    setAuthToken(response.access_token ?? null);
    await checkAuthStatus();
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
