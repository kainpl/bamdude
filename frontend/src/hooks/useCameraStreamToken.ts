import { useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  api,
  getMediaToken,
  getStreamToken,
  isCameraMediaPath,
  setMediaToken,
  setStreamToken,
} from '../api/client';
import { useAuth } from '../contexts/AuthContext';

/** `src` with its `token=` replaced by (or given) *token*. */
function stampToken(src: string, token: string): string {
  const withoutToken = src.replace(/([?&])token=[^&]*(&|$)/, (_m, pre, post) =>
    post === '&' ? pre : pre === '?' ? '' : ''
  );
  const sep = withoutToken.includes('?') ? '&' : '?';
  return `${withoutToken}${sep}token=${encodeURIComponent(token)}`;
}

/**
 * Walks the DOM and updates every <img>/<video> pointing at /api/v1/ so its
 * src carries the current token. Exported for unit testing; called from
 * useStreamTokenSync when a token arrives after first render.
 *
 * *applies* narrows it to the sources that take THIS token: the camera routes
 * take the camera token and every other picture the media token, and neither
 * accepts the other (audit D9 a2).
 */
export function rewriteMediaSrcWithToken(
  root: ParentNode,
  token: string,
  applies: (src: string) => boolean = () => true
): number {
  const tokenParam = `token=${encodeURIComponent(token)}`;
  let updated = 0;
  root
    .querySelectorAll<HTMLImageElement | HTMLVideoElement>(
      'img[src*="/api/v1/"], video[src*="/api/v1/"]'
    )
    .forEach((el) => {
      const src = el.getAttribute('src') || '';
      if (src.includes(tokenParam) || !applies(src)) return;
      el.src = stampToken(src, token);
      updated += 1;
    });
  return updated;
}

const isMediaPath = (src: string) => !isCameraMediaPath(src);

// Tokens last 60 minutes; refresh a little before.
const TOKEN_REFRESH_MS = 50 * 60 * 1000;

/**
 * Fetches and caches the two URL tokens for <img>/<video> src URLs and stores
 * them globally (setStreamToken / setMediaToken) so URL generators in client.ts
 * can use withStreamToken() / withMediaToken() automatically.
 *
 * - The MEDIA token — thumbnails, plates, covers, timelapses — for every
 *   signed-in user (audit D9 a2). What it reaches is the server's decision,
 *   per resource.
 * - The CAMERA token only for a user who may view the camera: asking without
 *   `camera:view` was a 403 on every page load for everyone else.
 *
 * Also listens for image/video load errors on token-protected URLs: a failure
 * that carries the current token refreshes it (a backend restart can drop
 * tokens), and a picture that rendered WITHOUT its token — a URL the server
 * handed over and nobody wrapped — is stamped once, so it recovers instead of
 * staying broken.
 *
 * Mount this hook once near the app root (e.g., in App.tsx or a layout component).
 */
export function useStreamTokenSync() {
  const { user, hasPermission } = useAuth();
  const queryClient = useQueryClient();
  const refreshingRef = useRef(false);
  const canViewCamera = !!user && hasPermission('camera:view');

  // Key the tokens by user id so a login/logout invalidates the cache
  // automatically — otherwise a failed anonymous fetch on the login page
  // would be cached and never retried after sign-in (upstream 32c0b169).
  const { data: streamData } = useQuery({
    queryKey: ['camera-stream-token', user?.id ?? null],
    queryFn: () => api.getCameraStreamToken(),
    enabled: canViewCamera,
    staleTime: TOKEN_REFRESH_MS,
    refetchInterval: TOKEN_REFRESH_MS,
  });
  const { data: mediaData } = useQuery({
    queryKey: ['media-token', user?.id ?? null],
    queryFn: () => api.getMediaToken(),
    enabled: !!user,
    staleTime: TOKEN_REFRESH_MS,
    refetchInterval: TOKEN_REFRESH_MS,
  });

  useEffect(() => {
    const newToken = streamData?.token ?? null;
    setStreamToken(newToken);
    // Images/videos that rendered before the token arrived have src URLs
    // without ?token=…; update them in place so they reload with auth.
    if (newToken) {
      rewriteMediaSrcWithToken(document, newToken, isCameraMediaPath);
    }
    return () => setStreamToken(null);
  }, [streamData?.token]);

  useEffect(() => {
    const newToken = mediaData?.token ?? null;
    setMediaToken(newToken);
    if (newToken) {
      rewriteMediaSrcWithToken(document, newToken, isMediaPath);
    }
    return () => setMediaToken(null);
  }, [mediaData?.token]);

  useEffect(() => {
    if (!user) return;

    const handleError = (event: Event) => {
      const el = event.target;
      if (!(el instanceof HTMLImageElement || el instanceof HTMLVideoElement)) return;

      const src = el.getAttribute('src') || '';
      if (!src.includes('/api/v1/')) return;
      const camera = isCameraMediaPath(src);
      const token = camera ? getStreamToken() : getMediaToken();
      if (!token) return;

      if (!src.includes(`token=${encodeURIComponent(token)}`)) {
        // Rendered without its token, or with the other one. Stamp it — once
        // per token, so a picture that fails for another reason cannot loop.
        if (el.dataset.tokenStamped === token) return;
        el.dataset.tokenStamped = token;
        el.src = stampToken(src, token);
        return;
      }

      // It carried the current token and still failed: the token may have
      // been dropped (backend restart). Refresh it, at most every 5 seconds.
      if (refreshingRef.current) return;
      refreshingRef.current = true;
      queryClient.invalidateQueries({ queryKey: [camera ? 'camera-stream-token' : 'media-token'] });
      setTimeout(() => {
        refreshingRef.current = false;
      }, 5000);
    };

    document.addEventListener('error', handleError, true);
    return () => document.removeEventListener('error', handleError, true);
  }, [user, queryClient]);
}
