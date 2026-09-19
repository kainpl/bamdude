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
 * The native arrow is kept deliberately. Twenty call sites drew their own with
 * `appearance-none`, but the reason to — a light triangle on a dark field — was
 * already solved globally by `color-scheme` in `index.css`, so a custom chevron
 * buys nothing and costs a wrapper element that would break `className`.
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

interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'size'> {
  /**
   * Heights, not paddings: a toolbar lines a select up with buttons and inputs,
   * and this codebase already says that as `h-9`. A `<select>` centres its one
   * line by itself, so a height is the honest knob. `md` is the toolbar and
   * form default; `xs` exists for a control embedded in a dense header row and
   * is the one size with no touch-target floor, because its row has no space
   * for one.
   *
   * ⚠️ This shadows the native `size` attribute (the number of visible rows).
   * Nothing in the app uses that, and `multiple` lists are not this component.
   */
  size?: SelectSize;
  /**
   * Which way the field reads against what is behind it. `sunken` is a field on
   * a card or panel, `raised` a field on the page's own ground, and `muted` is
   * a sunken one that sits among controls rather than in a form — the same idea
   * as `Button`'s `outline`: grey until you go near it.
   *
   * The text colour lives here rather than in the base, because passing
   * `text-bambu-gray` through `className` would be a coin toss — two utilities
   * of one family are settled by Tailwind's emit order, not by the attribute.
   */
  tone?: 'sunken' | 'raised' | 'muted';
  children: ReactNode;
}

export function Select({ size = 'md', tone = 'sunken', className = '', children, ...props }: SelectProps) {
  const baseStyles =
    'border border-bambu-dark-tertiary transition-colors focus:outline-none focus:border-bambu-green disabled:opacity-50 disabled:cursor-not-allowed';

  const tones = {
    sunken: 'bg-bambu-dark text-white',
    raised: 'bg-bambu-dark-secondary text-white',
    muted: 'bg-bambu-dark text-bambu-gray hover:text-white',
  };

  const sizes = {
    xs: 'h-6 px-2 text-xs rounded',
    sm: 'h-8 px-2 text-sm rounded-lg min-h-[44px] md:min-h-0',
    md: 'h-9 px-3 text-sm rounded-lg min-h-[44px] md:min-h-0',
    lg: 'h-11 px-3 text-base rounded-lg min-h-[48px] md:min-h-0',
  };

  return (
    <select className={`${baseStyles} ${tones[tone]} ${sizes[size]} ${className}`} {...props}>
      {children}
    </select>
  );
}
