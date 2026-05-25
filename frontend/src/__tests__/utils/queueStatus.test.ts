import { describe, it, expect } from 'vitest';
import { queueResumePayload } from '../../utils/queueStatus';

describe('queueResumePayload', () => {
  // The single Pause/Resume control replaced a separate error-banner button
  // that cleared only `status` — leaving `is_paused` set so a paused+errored
  // queue stayed stuck. Resume must clear BOTH gates.
  it('clears both gates when the queue is in error (error + operator-paused)', () => {
    expect(queueResumePayload('error')).toEqual({ is_paused: false, status: 'idle' });
  });

  it('clears both gates when the queue auto-paused after a cancel', () => {
    expect(queueResumePayload('paused')).toEqual({ is_paused: false, status: 'idle' });
  });

  it('clears only the operator pause when status is printing/idle', () => {
    expect(queueResumePayload('printing')).toEqual({ is_paused: false });
    expect(queueResumePayload('idle')).toEqual({ is_paused: false });
  });
});
