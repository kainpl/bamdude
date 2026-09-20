import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { api } from '../api/client';

/**
 * The picture to draw for one queue row — the job's OWN render first (m173).
 *
 * A job keeps an immutable copy of the bytes it prints, so it still prints once
 * its library file or archive is deleted (spec §4, A09) — and it has to be able
 * to show itself after that too, which no `archive_id` / `library_file_id` URL
 * can do. `itemId` is the queue row whose snapshot renders its plate, which the
 * list says with `source_thumbnail`; pass `null` when it says no and the
 * original's URL (or `null`) is used instead.
 *
 * ⚠️ **The job's own render outranks the original's thumbnail**, not the other
 * way round: the original may have been re-sliced since this job accepted its
 * bytes, and the job prints the bytes it accepted (A03). That is the same
 * precedence the API applies to the row's estimate, weight and build plate.
 *
 * ⚠️ **A fetch, not a URL.** The route is behind the queue's own
 * `read_all`/`read_own` split, and an `<img src>` cannot carry a bearer token —
 * so the bytes come back as a Blob and `<img>` gets an object URL, which this
 * revokes when the row goes away or its picture changes. One leaked URL per
 * rendered row for the life of the tab is what the alternative costs.
 *
 * A missing picture is not an error: `retry: false`, and a 404 simply leaves the
 * caller drawing its empty state. Nothing here shows a placeholder that implies
 * a picture exists.
 */
export function useQueueRowPicture(itemId: number | null, fallbackUrl: string | null = null): string | null {
  const { data: blob } = useQuery({
    queryKey: ['queue-source-thumbnail', itemId],
    queryFn: () => api.getQueueItemSourceThumbnail(itemId as number),
    enabled: itemId != null,
    // The bytes are content-addressed and immutable, so a row's picture never
    // needs re-fetching while the tab is open.
    staleTime: Infinity,
    retry: false,
  });

  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!blob) {
      setObjectUrl(null);
      return;
    }
    const url = URL.createObjectURL(blob);
    setObjectUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [blob]);

  return objectUrl ?? fallbackUrl;
}
