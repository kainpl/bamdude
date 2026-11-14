import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';

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
 * A logo that cannot be fetched or drawn is dropped until its revision changes:
 * a broken image must not sit in a broadcast.
 */
export function useOverlayLogo(revision: string | null | undefined, token: string | null): OverlayLogo | null {
  const wanted = revision ? `${revision}\n${token ?? ''}` : null;
  // The fetched picture, and what failed — each remembered with what it belongs
  // to, so a new revision (or token) starts clean without an effect resetting it.
  const [fetched, setFetched] = useState<{ wanted: string; url: string } | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    if (!wanted || !revision) return;
    let cancelled = false;
    let url: string | undefined;
    (token ? api.getOverlayLogoWithToken(revision, token) : api.getOverlayLogo(revision)).then(
      (blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setFetched({ wanted, url });
      },
      () => {
        if (!cancelled) setFailed(wanted);
      },
    );
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [wanted, revision, token]);

  const onError = useCallback(() => setFailed(wanted), [wanted]);

  if (!wanted || failed === wanted || fetched?.wanted !== wanted) return null;
  return { src: fetched.url, onError };
}
