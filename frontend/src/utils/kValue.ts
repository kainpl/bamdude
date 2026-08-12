/**
 * Pressure-advance K: bounds and display formatting.
 *
 * Lives outside the component so it can be tested directly, and because
 * exporting non-components from a component file breaks fast refresh.
 */

/**
 * Bambu Studio's bounds, from `CalibUtils::validate_input_k_value`:
 * `MIN_PA_K_VALUE = 0.0`, `MAX_PA_K_VALUE = 2.0`, and the check is
 * `k <= MIN || k >= MAX` — so both ends are exclusive.
 */
export const MIN_PA_K_VALUE = 0.0;
export const MAX_PA_K_VALUE = 2.0;

/** Empty, unparseable and out-of-range are all invalid, exactly as in BS. */
export const isValidKValue = (value: string): boolean => {
  if (value.trim() === '') return false;
  const num = Number(value);
  return Number.isFinite(num) && num > MIN_PA_K_VALUE && num < MAX_PA_K_VALUE;
};

/**
 * Three decimals for DISPLAY — all BS does with it (`%.3f` in
 * `CalibrationWizardSavePage`). It is not a transformation of the stored value.
 *
 * ⚠️ Rounds, never truncates. The previous `Math.trunc(num * 1000) / 1000` ran
 * on input rather than on display and turned a real 0.0005 into 0.000 — a saved
 * profile with pressure advance switched off, silently. It claimed to be "like
 * Bambu Studio"; BS does no such thing.
 */
export const formatKForDisplay = (value: number | string): string => {
  const num = typeof value === 'number' ? value : parseFloat(value);
  return Number.isFinite(num) ? num.toFixed(3) : '';
};
