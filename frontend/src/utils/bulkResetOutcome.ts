/**
 * What a bulk "Reset counter" answer amounts to.
 *
 * Spoolman is a remote service where single spools can fail, so its route
 * lists the failures in `errors`; the built-in route names ids that matched
 * nothing in `not_found`. The toast branches on all / some / none instead of
 * always claiming success — the same three outcomes the other bulk actions
 * report.
 */
import type { BulkResetResult } from '../api/client';

export interface BulkResetOutcome {
  kind: 'ok' | 'partial' | 'failed';
  ok: number;
  failed: number;
}

export function bulkResetOutcome(result: BulkResetResult): BulkResetOutcome {
  const ok = result.reset;
  const failed = (result.errors?.length ?? 0) + (result.not_found?.length ?? 0);
  const kind = failed === 0 ? 'ok' : ok > 0 ? 'partial' : 'failed';
  return { kind, ok, failed };
}
