export interface AppliedEntry {
  /** What the device answered to configure_reporting. */
  state: 'ok' | 'refused' | 'unanswered' | 'unknown' | string;
  /** What reading the configuration back said. */
  verification: 'verified' | 'mismatch' | 'not-checked' | string;
  /** The settings this outcome is about, in the operator's units. */
  values: { min_interval: number; max_interval: number; reportable_change: number } | null;
  /** What the device stored, in RAW units. Null unless a read-back happened. */
  actual: Record<string, number> | null;
  at: string | null;
  /** Whether this outcome is about the settings currently on screen. */
  describes_desired: boolean;
}

export type ReportingStatus =
  | 'pending'
  | 'unknown'
  | 'refused'
  | 'unanswered'
  | 'mismatch'
  | 'verified'
  | 'unchecked';

/**
 * Nine combinations and a flag, as one sentence.
 *
 * ⚠️ The ORDER is the design. `describes_desired` comes first because every
 * other fact describes settings other than the ones on screen — an outcome of
 * ok + verified about the previous values must not read as confirmation of the
 * new ones. Swapping two branches here produces a dialog that is confidently
 * wrong in exactly the case this vocabulary was built for, and a diff that
 * looks like a tidy-up.
 */
export function reportingStatus(entry: AppliedEntry): ReportingStatus {
  if (!entry.describes_desired) return 'pending';
  if (entry.state === 'unknown') return 'unknown';
  if (entry.state === 'refused') return 'refused';
  if (entry.state === 'unanswered') return 'unanswered';
  if (entry.verification === 'mismatch') return 'mismatch';
  if (entry.verification === 'verified') return 'verified';
  return 'unchecked';
}

export const REPORTING_STATUS_KEY: Record<ReportingStatus, string> = {
  pending: 'settings.zigbee.reporting.pending',
  unknown: 'settings.zigbee.reporting.unknown',
  refused: 'settings.zigbee.reporting.refused',
  unanswered: 'settings.zigbee.reporting.unanswered',
  mismatch: 'settings.zigbee.reporting.mismatch',
  verified: 'settings.zigbee.reporting.verified',
  unchecked: 'settings.zigbee.reporting.unchecked',
};
