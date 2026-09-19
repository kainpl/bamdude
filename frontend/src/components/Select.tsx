import type { ReactNode, SelectHTMLAttributes } from 'react';

/**
 * The one dropdown field.
 *
 * Before this, "pick one from a list" was written from scratch at every call
 * site: an inventory on 1bc637e7 found 207 native `<select>`s carrying 75
 * different sets of classes, disagreeing on radius, background, padding, font
 * size, focus ring and even whether to draw their own arrow. An earlier attempt
 * to settle it with a shared class string (`FIELD_CLASS`, `inputCls`, …) was
 * copied into a dozen files in seven different values — which is why this is a
 * component and not another constant.
 *
 * It stays a real `<select>`: on a phone that means the system picker, and half
 * the farm is driven from a phone. A control that needs icons, checkboxes or
 * arbitrary markup in a row is a different widget and does not belong here.
 *
 * The arrow is ours, drawn by the `select-chevron` utility in `index.css` as a
 * background image so this stays ONE element — a wrapper would swallow the
 * `className` every call site passes. It replaced the native arrow once it was
 * measured that the native one cannot be given room: Chrome puts it hard
 * against the border whatever `padding-right` says. (Visibility was never the
 * reason — `color-scheme` had already settled that.)
 *
 * ⚠️ In a flex row, give it `min-w-0`. A form control's `min-width: auto` is
 * its widest option, so without that it refuses to shrink and pushes whatever
 * sits beside it out of the container — and the widest option is a translated
 * string, i.e. a number this side of the code does not control. The floor, when
 * one is wanted, is the caller's `min-w-[…]`: it is not in the base here,
 * because two utilities of one family are settled by Tailwind's emit order
 * rather than by the class attribute, so a base `min-w-0` might quietly win
 * over the call site's own minimum.
 */

type SelectSize = 'xs' | 'sm' | 'md' | 'lg';

/**
 * Heights, not paddings: a toolbar lines a select up with buttons and inputs,
 * and this codebase already says that as `h-9`. A `<select>` centres its one
 * line by itself, so a height is the honest knob. `md` is the toolbar and form
 * default; `xs` exists for a control embedded in a dense header row and is the
 * one size with no touch-target floor, because its row has no space for one.
 */
const SIZES: Record<SelectSize, string> = {
  xs: 'h-6 pl-2 pr-7 text-xs rounded',
  sm: 'h-8 pl-2 pr-7 text-sm rounded-lg min-h-[44px] md:min-h-0',
  md: 'h-9 pl-3 pr-8 text-sm rounded-lg min-h-[44px] md:min-h-0',
  lg: 'h-11 pl-3 pr-8 text-base rounded-lg min-h-[48px] md:min-h-0',
};

/**
 * How the field reads against what is behind it. `sunken` is a field on a card
 * or panel, `raised` a field on the page's own ground, and `muted` a sunken one
 * that sits among controls rather than in a form — the same idea as `Button`'s
 * `outline`: grey until you go near it.
 *
 * The text colour lives here rather than in the base, because passing
 * `text-bambu-gray` through `className` would be a coin toss — two utilities of
 * one family are settled by Tailwind's emit order, not by the attribute.
 */
const TONES = {
  sunken: 'bg-bambu-dark text-white',
  raised: 'bg-bambu-dark-secondary text-white',
  muted: 'bg-bambu-dark text-bambu-gray hover:text-white',
} as const;

/**
 * A filter chip is not a field, so it does not take a `size`: it has one shape,
 * sized to the toggle buttons it shares a row with, and its whole point is that
 * you can see from across the room whether it is set. That state is `active`,
 * which the type demands here and forbids everywhere else — a filter that
 * forgot to say when it is on looks permanently empty, which is the failure
 * this control exists to prevent.
 */
const FILTER_SHAPE = 'h-7 pl-3 pr-7 text-xs font-medium rounded-lg';
const FILTER_STATE = {
  on: 'bg-bambu-green/20 text-bambu-green border-bambu-green/30',
  off: 'bg-transparent text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary',
};

type FieldProps = {
  size?: SelectSize;
  tone?: keyof typeof TONES;
  active?: never;
};

type FilterProps = {
  size?: never;
  tone: 'filter';
  active: boolean;
};

/**
 * ⚠️ `size` shadows the native attribute (the number of visible rows). Nothing
 * in the app uses that, and `multiple` lists are not this component.
 */
type SelectProps = Omit<SelectHTMLAttributes<HTMLSelectElement>, 'size'> &
  (FieldProps | FilterProps) & { children: ReactNode };

export function Select({ size, tone = 'sunken', active, className = '', children, ...props }: SelectProps) {
  const baseStyles =
    'select-chevron border transition-colors focus:outline-none focus:border-bambu-green disabled:opacity-50 disabled:cursor-not-allowed';

  const look =
    tone === 'filter'
      ? `${FILTER_SHAPE} ${active ? FILTER_STATE.on : FILTER_STATE.off}`
      : `border-bambu-dark-tertiary ${TONES[tone]} ${SIZES[size ?? 'md']}`;

  return (
    <select className={`${baseStyles} ${look} ${className}`} {...props}>
      {children}
    </select>
  );
}
