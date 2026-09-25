// The blocker to name when an AMS reports several dry_sf_reason codes — the same
// priority as the backend's first_drying_blocking_reason: power (1, 8), then
// filament at the outlet (3), then whatever else. Power and the outlet need the
// operator to act; the rest clear on their own (upstream d37ce94f).
const POWER = new Set([1, 8]);
const RETRACT = 3;

export function dryingBlockedKey(reasons?: number[] | null): string {
  const codes = reasons ?? [];
  if (codes.some((c) => POWER.has(c))) return 'printers.drying.blockedPower';
  if (codes.includes(RETRACT)) return 'printers.drying.blockedRetract';
  return 'printers.drying.blockedOther';
}
