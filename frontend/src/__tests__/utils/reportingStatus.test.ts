import { describe, expect, it } from 'vitest';

import { REPORTING_STATUS_KEY, reportingStatus } from '../../utils/reportingStatus';
import type { AppliedEntry } from '../../utils/reportingStatus';

function entry(over: Partial<AppliedEntry> = {}): AppliedEntry {
  return {
    state: 'ok',
    verification: 'verified',
    values: { min_interval: 30, max_interval: 900, reportable_change: 0.1 },
    actual: null,
    at: '2026-08-03T10:00:00+00:00',
    describes_desired: true,
    ...over,
  };
}

describe('reportingStatus', () => {
  it('an outcome about other values beats every other fact', () => {
    // The whole reason `values` and `describes_desired` exist. A sleeping
    // sensor keeps the previous outcome -- true, and about the old settings --
    // and showing its tick beside the new ones is the failure this prevents.
    expect(reportingStatus(entry({ describes_desired: false }))).toBe('pending');
    expect(reportingStatus(entry({ describes_desired: false, state: 'ok', verification: 'verified' }))).toBe(
      'pending',
    );
  });

  it('nothing has been asked of a device we have not spoken to', () => {
    expect(reportingStatus(entry({ state: 'unknown', verification: 'not-checked' }))).toBe('unknown');
  });

  it('a refusal is not a silence', () => {
    expect(reportingStatus(entry({ state: 'refused', verification: 'not-checked' }))).toBe('refused');
    expect(reportingStatus(entry({ state: 'unanswered', verification: 'not-checked' }))).toBe('unanswered');
  });

  it('the device storing something else outranks it having accepted', () => {
    // configure_reporting answered SUCCESS and the read-back disagreed. The
    // acceptance is real and useless; the mismatch is what needs acting on.
    expect(reportingStatus(entry({ state: 'ok', verification: 'mismatch' }))).toBe('mismatch');
  });

  it('accepted and confirmed, and accepted and unconfirmed, are different', () => {
    expect(reportingStatus(entry({ state: 'ok', verification: 'verified' }))).toBe('verified');
    expect(reportingStatus(entry({ state: 'ok', verification: 'not-checked' }))).toBe('unchecked');
  });

  it('every status has a string', () => {
    // A status with no key renders as a raw token in front of an operator.
    const all = ['pending', 'unknown', 'refused', 'unanswered', 'mismatch', 'verified', 'unchecked'] as const;
    for (const status of all) {
      expect(REPORTING_STATUS_KEY[status]).toMatch(/^settings\.zigbee\.reporting\./);
    }
  });
});
