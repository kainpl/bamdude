import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';

export interface OverlayLogo {
  src: string;
  /** The picture could not be drawn: the overlay takes it down. */
  onError: () => void;
}

/**
 * The installation's logo for the stream overlay, ready for an `<img>`.
 *
 * `revision` is what the server said is saved — null when there is no logo, or
 * the overlay's address did not ask for one; nothing is requested then.
 *
 * - With a token (OBS — no session): the overlay's own logo route, under the
 *   same token as its status feed, the revision in the address before the token.
 * - Without one (a signed-in page): the session's route.
 *
 * Either way the bytes are fetched and shown from a `blob:` URL, never as an
 * `<img src>` on the API. ⚠️ Not a matter of taste: `useStreamTokenSync` stamps
 * the signed-in user's MEDIA token onto every `/api/v1/` image it finds — in a
 * signed-in browser (the builder's preview) that would replace the overlay
 * token in the address, and the overlay route refuses a media token.
 *
 * A logo the server REFUSES (401 / 403 / 404) or that will not decode is dropped
 * until its revision changes: a broken image must not sit in a broadcast. Any
 * other failure — the server restarting, the network — is a blip in a page
 * that stays open for a whole broadcast, and is asked again after a pause that
 * grows (`OVERLAY_LOGO_RETRY_MS`).
 */
/** The pauses before the logo is asked for again after a transient failure;
 *  the last one repeats for as long as the overlay stays open. */
export const OVERLAY_LOGO_RETRY_MS = [30_000, 60_000, 120_000, 300_000] as const;

const refused = (error: unknown) => error instanceof ApiError && [401, 403, 404].includes(error.status);

export function useOverlayLogo(revision: string | null | undefined, token: string | null): OverlayLogo | null {
  const wanted = revision ? `${revision}\n${token ?? ''}` : null;
  // The fetched picture, and what failed — each remembered with what it belongs
  // to, so a new revision (or token) starts clean without an effect resetting it.
  const [fetched, setFetched] = useState<{ wanted: string; url: string } | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  // Transient failures in a row for what is wanted now: each one asks again.
  const [retry, setRetry] = useState<{ wanted: string; count: number } | null>(null);
  const attempt = retry?.wanted === wanted ? retry.count : 0;

  useEffect(() => {
    if (!wanted || !revision) return;
    let cancelled = false;
    let url: string | undefined;
    let again: ReturnType<typeof setTimeout> | undefined;
    (token ? api.getOverlayLogoWithToken(revision, token) : api.getOverlayLogo(revision)).then(
      (blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setFetched({ wanted, url });
      },
      (error: unknown) => {
        if (cancelled) return;
        if (refused(error)) {
          setFailed(wanted);
          return;
        }
        const pause = OVERLAY_LOGO_RETRY_MS[Math.min(attempt, OVERLAY_LOGO_RETRY_MS.length - 1)];
        again = setTimeout(() => setRetry({ wanted, count: attempt + 1 }), pause);
      },
    );
    return () => {
      cancelled = true;
      clearTimeout(again);
      if (url) URL.revokeObjectURL(url);
    };
  }, [wanted, revision, token, attempt]);

  const onError = useCallback(() => setFailed(wanted), [wanted]);

  if (!wanted || failed === wanted || fetched?.wanted !== wanted) return null;
  return { src: fetched.url, onError };
}
