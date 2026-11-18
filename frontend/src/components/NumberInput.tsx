import { useState, type InputHTMLAttributes } from 'react';

type NativeInputProps = Omit<
  InputHTMLAttributes<HTMLInputElement>,
  'type' | 'value' | 'defaultValue' | 'onChange' | 'min' | 'max'
>;

interface BaseProps extends NativeInputProps {
  /** The saved value. Shown whenever the field is not being edited. */
  value: number | null | undefined;
  min?: number;
  max?: number;
  /** Whole numbers only (parseInt). Without it the draft is read with parseFloat. */
  integer?: boolean;
  /**
   * Reported when the field is left empty. Without it an empty field goes back
   * to `value`. Clamped into [min, max] like anything else.
   */
  fallback?: number;
}

interface RequiredProps extends BaseProps {
  allowEmpty?: false;
  /** Every time the draft is a number inside [min, max], and once more on blur when it had to be settled. */
  onChange?: (value: number) => void;
  /** Once per edit, on blur or Enter, with the settled value — and only when it differs from the value the edit started from. */
  onCommit?: (value: number) => void;
}

interface NullableProps extends BaseProps {
  /** An empty field is a value of its own: `null` is reported. */
  allowEmpty: true;
  onChange?: (value: number | null) => void;
  onCommit?: (value: number | null) => void;
}

export type NumberInputProps = RequiredProps | NullableProps;

/**
 * A number field that can be cleared and retyped.
 *
 * Parsing and clamping inside `onChange` rewrites the field on every
 * keystroke: an emptied field springs back to its default, the "6" of 60 in a
 * field with a minimum of 45 becomes 45, and `|| default` refuses a legal 0.
 * This keeps what was typed as a draft while the field is edited, reports a
 * value only once the draft is a number inside [min, max], and settles the
 * field on blur or Enter: an out-of-range number is clamped and reported, an
 * empty or unreadable draft goes back to `fallback`, to `null` for a nullable
 * field, or else to the last value.
 *
 * `onCommit` is for a field whose change costs something (a request): it fires
 * once when the edit ends, never per key.
 */
export function NumberInput(props: NumberInputProps) {
  const {
    value,
    min,
    max,
    integer = false,
    fallback,
    allowEmpty,
    onChange,
    onCommit,
    onBlur,
    onKeyDown,
    ...rest
  } = props;
  // What is being typed, with the value it belongs to: `base` is the value the
  // field expects to hold while this text is on screen (what it last reported,
  // or the value it had when nothing was reported), and `start` the value the
  // edit began from, so `onCommit` stays silent for an edit that ended where it
  // began.
  const [edit, setEdit] = useState<{ text: string; base: number | null; start: number | null } | null>(null);

  const report = onChange as ((v: number | null) => void) | undefined;
  const commit = onCommit as ((v: number | null) => void) | undefined;
  const current = value ?? null;
  // A value that moved under the text — a refetch, a button beside the field, a
  // parent that refused or rounded what was reported — wins over the draft. So
  // does `disabled`: a field disabled mid-edit gets no blur.
  const live = edit !== null && edit.base === current && !rest.disabled ? edit : null;
  const draft = live?.text ?? null;

  const parse = (raw: string) => (integer ? parseInt(raw, 10) : parseFloat(raw));
  const inRange = (n: number) => (min === undefined || n >= min) && (max === undefined || n <= max);
  const clamp = (n: number) => Math.min(max ?? Infinity, Math.max(min ?? -Infinity, n));

  const settle = () => {
    if (edit === null) return;
    setEdit(null);
    if (live === null || draft === null) return;
    const n = parse(draft);
    let settled: number | null;
    if (!Number.isNaN(n)) settled = clamp(n);
    else if (allowEmpty && draft.trim() === '') settled = null;
    else if (fallback !== undefined) settled = clamp(fallback);
    else settled = current;
    if (settled !== current && (settled !== null || allowEmpty)) report?.(settled);
    if (settled !== live.start && (settled !== null || allowEmpty)) commit?.(settled);
  };

  return (
    <input
      {...rest}
      type="number"
      min={min}
      max={max}
      value={draft ?? value ?? ''}
      onChange={(e) => {
        const raw = e.target.value;
        const start = live ? live.start : current;
        let next: number | null = current;
        if (raw.trim() === '') {
          if (allowEmpty) next = null;
        } else {
          const n = parse(raw);
          if (!Number.isNaN(n) && inRange(n)) next = n;
        }
        if (next !== current && report) {
          report(next);
          setEdit({ text: raw, base: next, start });
        } else {
          setEdit({ text: raw, base: current, start });
        }
      }}
      onBlur={(e) => {
        settle();
        onBlur?.(e);
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') settle();
        onKeyDown?.(e);
      }}
    />
  );
}
