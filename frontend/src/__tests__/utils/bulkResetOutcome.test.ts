import { describe, expect, it } from 'vitest';
import { bulkResetOutcome } from '../../utils/bulkResetOutcome';

describe('bulkResetOutcome', () => {
  it('is a success when every spool was reset', () => {
    expect(bulkResetOutcome({ reset: 3, errors: [] })).toEqual({ kind: 'ok', ok: 3, failed: 0 });
  });

  it('reads an answer without a failure list as a success (older server)', () => {
    expect(bulkResetOutcome({ reset: 2 })).toEqual({ kind: 'ok', ok: 2, failed: 0 });
  });

  it('is partial when Spoolman refused some of the spools', () => {
    const result = { reset: 2, errors: [{ id: 9, status: 404, detail: 'Spool not found in Spoolman' }] };
    expect(bulkResetOutcome(result)).toEqual({ kind: 'partial', ok: 2, failed: 1 });
  });

  it('is a failure, not a green zero, when nothing was reset', () => {
    const result = {
      reset: 0,
      errors: [
        { id: 8, status: 503, detail: 'Spoolman server is not reachable' },
        { id: 9, status: 503, detail: 'Spoolman server is not reachable' },
      ],
    };
    expect(bulkResetOutcome(result)).toEqual({ kind: 'failed', ok: 0, failed: 2 });
  });

  it('counts built-in ids that matched nothing as failures', () => {
    expect(bulkResetOutcome({ reset: 1, not_found: [7] })).toEqual({ kind: 'partial', ok: 1, failed: 1 });
  });
});
