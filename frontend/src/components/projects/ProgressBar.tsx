interface ProgressBarProps {
  value: number;
  max: number;
  label?: string;
  testId?: string;
}

/**
 * Progress as the server counted it.
 *
 * Renders nothing when there is nothing to count against — the `{0 && <jsx>}`
 * stray-zero bug lived exactly here, so the gate is an explicit `<= 0` return
 * and never a `&&` on a number. The `value / max` caption is rendered ONLY
 * inside this component: a caller that prints its own numbers beside the bar is
 * the second source of truth this component exists to remove.
 */
export function ProgressBar({ value, max, label, testId = 'progress' }: ProgressBarProps) {
  if (max <= 0) return null;
  const pct = Math.min(100, Math.round((value / max) * 100));
  return (
    <div data-testid={testId} className="space-y-1">
      <div className="flex items-center justify-between text-xs text-bambu-gray">
        {label ? <span>{label}</span> : <span />}
        <span className="tabular-nums">{`${value} / ${max}`}</span>
      </div>
      <div className="h-2 rounded-full bg-bambu-dark-tertiary overflow-hidden">
        <div
          data-testid={`${testId}-fill`}
          className="h-full bg-bambu-green transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
