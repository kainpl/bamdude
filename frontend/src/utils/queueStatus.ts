// Queue Pause/Resume helpers.
//
// A printer queue can be halted to dispatch by TWO orthogonal signals:
//   - `is_paused`            — the operator's explicit pause toggle.
//   - `status` ∈ paused|error — auto-pause after a user cancel, or a fault.
// The scheduler skips dispatch when EITHER is set (see print_scheduler), so a
// single "Resume" control must clear BOTH at once. The old error banner had its
// own button that cleared only `status`, leaving `is_paused` set — so a queue
// that was both paused and errored stayed stuck after the operator "resumed" it.

/**
 * Build the update payload for resuming a queue. Clears the operator pause and,
 * when the queue also carries a status-level paused/error state, resets that to
 * idle too — so one control fully restarts dispatch.
 */
export function queueResumePayload(status: string): { is_paused: boolean; status?: 'idle' } {
  return status === 'paused' || status === 'error'
    ? { is_paused: false, status: 'idle' }
    : { is_paused: false };
}
