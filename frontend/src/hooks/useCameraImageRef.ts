import { useCallback, useRef, useSyncExternalStore } from 'react';
import { getStreamToken, subscribeStreamToken } from '../api/client';

export const EMPTY_CAMERA_IMAGE = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

/** True for an image that shows no camera: its `load` or `error` says nothing
 * about a stream. A view that empties its own image (`src = ''` fires `error`,
 * the placeholder fires `load`) must not read that as the camera's answer. */
export function isIdleCameraImage(image: HTMLImageElement): boolean {
  const src = image.getAttribute('src');
  return !src || src === EMPTY_CAMERA_IMAGE;
}

/** The images whose address this hook writes. Nothing else may: the token
 *  sync, which stamps a fresh token onto every camera `<img>` on the page,
 *  leaves these to their owner. Kept beside the DOM, not in it — an attribute
 *  would be a change to the markup an OBS scene loads. */
const owned = new WeakSet<Element>();

/** True for an image `useCameraImageRef` writes the address of. */
export function isOwnedCameraImage(image: Element): boolean {
  return owned.has(image);
}

/** The ONE writer of a camera image's `src`.
 *
 * An MJPEG `<img>` starts loading the moment it is given its URL, attached to
 * the page or not. React sets props while it renders, so a `src` prop also
 * lands on the image of a render React throws away (a suspended or interrupted
 * transition, StrictMode's replay) — a stream nothing can close. The URL is
 * therefore assigned here, from the ref callback of a mounted node, and never
 * passed as a prop (`cameraStreamSrcOwnership.test.ts`).
 *
 * The cleanup cancels the actual detached node, including refresh/key
 * replacement and minimization. An effect capturing the first img misses its
 * replacements.
 *
 * A refreshed stream token is a new address, and the server ends a stream when
 * the token it was opened with expires. The owner builds its URL from the
 * token while it renders, so this hook re-renders it when the token changes:
 * the image is then given its new address here, once — never also by the token
 * sync, which used to stamp it first (two restarts of one stream). */
export function useCameraImageRef(url: string) {
  // Read for the re-render alone; the owner's `url` already carries the token.
  useSyncExternalStore(subscribeStreamToken, getStreamToken);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const attachImage = useCallback((image: HTMLImageElement | null) => {
    if (!image) return;
    imageRef.current = image;
    owned.add(image);
    // StrictMode replays attachment; setup must restore what cleanup cleared.
    image.src = url || EMPTY_CAMERA_IMAGE;
    return () => {
      image.src = EMPTY_CAMERA_IMAGE;
      if (imageRef.current === image) imageRef.current = null;
    };
  }, [url]);
  return { imageRef, attachImage };
}
