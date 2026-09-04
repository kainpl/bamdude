import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { OrderCandidate } from '../api/client';

/**
 * The orders that could take this plate, from the ONE definition of that query.
 *
 * ⚠️ **The plate is part of the key.** Two plates of one 3MF are two different
 * questions — they yield different parts, so they answer to different lines,
 * and they can even belong to different products. Keying on the file alone
 * would serve plate 1's orders to plate 2's picker, silently, and the operator
 * would file a print under a line that never wanted it.
 *
 * `enabled` is not a nicety either: PrintModal mounts once per member of a
 * grouped run, and all but one of those members never render. A list nobody
 * looks at is not worth a request per file, so the dialog says when it is
 * actually asking.
 *
 * `staleTime` is 30 s because the number in the picker is the plan block's own
 * number: it moves whenever anything is queued or finishes, and a dialog opened
 * a minute later must not still show the count from before.
 */
export function useOrderCandidates(fileId: number | undefined, plateIndex: number, enabled: boolean) {
  return useQuery<OrderCandidate[]>({
    queryKey: ['order-candidates', fileId, plateIndex],
    queryFn: () => api.getOrderCandidates(fileId as number, plateIndex),
    enabled: enabled && Number.isFinite(fileId),
    staleTime: 30_000,
    // A file whose candidates cannot be fetched is a file printed without an
    // order, which is what the dialog did before this field existed. Retrying
    // would only delay a dialog the operator is waiting on.
    retry: false,
  });
}
