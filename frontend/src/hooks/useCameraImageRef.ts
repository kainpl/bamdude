import { useCallback, useRef } from 'react';

export const EMPTY_CAMERA_IMAGE = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

/** True for an image that shows no camera: its `load` or `error` says nothing
 * about a stream. A view that empties its own image (`src = ''` fires `error`,
 * the placeholder fires `load`) must not read that as the camera's answer. */
export function isIdleCameraImage(image: HTMLImageElement): boolean {
  const src = image.getAttribute('src');
  return !src || src === EMPTY_CAMERA_IMAGE;
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
 * replacements. */
export function useCameraImageRef(url: string) {
  const imageRef = useRef<HTMLImageElement | null>(null);
  const attachImage = useCallback((image: HTMLImageElement | null) => {
    if (!image) return;
    imageRef.current = image;
    // StrictMode replays attachment; setup must restore what cleanup cleared.
    image.src = url || EMPTY_CAMERA_IMAGE;
    return () => {
      image.src = EMPTY_CAMERA_IMAGE;
      if (imageRef.current === image) imageRef.current = null;
    };
  }, [url]);
  return { imageRef, attachImage };
}
