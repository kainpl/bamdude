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
 * `enabled` is the GATE, not a performance hint: it is false wherever the
 * dialog must not ask at all — the order page's plan block (which named its
 * line already), a reprint from an archive (which carries the original print's
 * binding), and the edit modes (whose update payloads have no project fields).
 * Those fire no request, and the answer could only mislead them if they did.
 * ⚠️ The silent members of a grouped run are NOT in that set: they never
 * render, but they do ask, and they wait for the answer before submitting —
 * each files itself under its own file's order.
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
