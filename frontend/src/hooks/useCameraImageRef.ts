import { useCallback, useRef } from 'react';

export const EMPTY_CAMERA_IMAGE = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

/** Cancel the actual detached node, including refresh/key replacement and
 * minimization. An effect capturing the first img misses its replacements. */
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
