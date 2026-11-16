import { Search } from 'lucide-react';
import {
  Children,
  Fragment,
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
  type SelectHTMLAttributes,
} from 'react';

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
 *
 * ⚠️ `search` is the one case that is NOT a single element. A list of a
 * thousand presets needs narrowing, and the answer that keeps the system
 * picker is a text box that filters the `<option>`s of the same real
 * `<select>`. Asked for, the field becomes a small group — the select, a
 * search button beside it and, while searching, a box above — and `className`
 * then sizes that group (the select fills it). Without `search` nothing
 * changes: one `<select>`, as every other call site has it.
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

/**
 * The words of the search control. They are the caller's — this primitive has
 * no translations of its own — and each names the control for a screen reader,
 * so pass them already saying WHICH list ("Search printer profiles").
 */
export interface SelectSearch {
  /** Accessible name of the search button and of the search box. */
  label: string;
  /** Hint inside the empty box. */
  placeholder?: string;
  /** Shown, and announced, when the query matches no option. */
  noMatches: string;
}

type FieldProps = {
  size?: SelectSize;
  tone?: keyof typeof TONES;
  active?: never;
  /** Turns the field into a searchable group; see the note above. */
  search?: SelectSearch;
};

type FilterProps = {
  size?: never;
  tone: 'filter';
  active: boolean;
  search?: never;
};

/**
 * ⚠️ `size` shadows the native attribute (the number of visible rows). Nothing
 * in the app uses that, and `multiple` lists are not this component.
 */
type SelectProps = Omit<SelectHTMLAttributes<HTMLSelectElement>, 'size'> &
  (FieldProps | FilterProps) & { children: ReactNode };

/** Heights of the search button, so it lines up with the select beside it. */
const SEARCH_BUTTON_SIZES: Record<SelectSize, string> = {
  xs: 'h-6 w-6 rounded',
  sm: 'h-8 w-8 rounded-lg min-h-[44px] min-w-[44px] md:min-h-0 md:min-w-0',
  md: 'h-9 w-9 rounded-lg min-h-[44px] min-w-[44px] md:min-h-0 md:min-w-0',
  lg: 'h-11 w-11 rounded-lg min-h-[48px] min-w-[48px] md:min-h-0 md:min-w-0',
};

type OptionProps = { value?: unknown; children?: ReactNode };

function textOf(node: ReactNode): string {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (isValidElement(node)) return textOf((node.props as OptionProps).children);
  return '';
}

/**
 * The `<option>`s that survive a query, with their `<optgroup>`s; a group left
 * empty goes. Search narrows what is LISTED, never what is chosen: the option
 * holding the current value always stays, and so does a valueless placeholder.
 * `matched` collects the values that matched by their text, in list order.
 */
function filterOptions(children: ReactNode, words: string[], current: string, matched: string[]): ReactNode {
  return Children.map(children, (child) => {
    if (!isValidElement(child)) return child;
    const element = child as ReactElement<OptionProps>;
    if (element.type === 'option') {
      const value = String(element.props.value ?? textOf(element.props.children));
      const text = textOf(element.props.children).toLowerCase();
      const hit = value !== '' && words.every((word) => text.includes(word));
      if (hit) matched.push(value);
      return hit || value === '' || value === current ? child : null;
    }
    if (element.type === 'optgroup' || element.type === Fragment) {
      const kept = filterOptions(element.props.children, words, current, matched);
      if (element.type === Fragment) return kept;
      return Children.count(kept) > 0 ? cloneElement(element, undefined, kept) : null;
    }
    return child;
  });
}

export function Select({ size, tone = 'sunken', active, className = '', children, search, ...props }: SelectProps) {
  const baseStyles =
    'select-chevron border transition-colors focus:outline-none focus:border-bambu-green disabled:opacity-50 disabled:cursor-not-allowed';

  const look =
    tone === 'filter'
      ? `${FILTER_SHAPE} ${active ? FILTER_STATE.on : FILTER_STATE.off}`
      : `border-bambu-dark-tertiary ${TONES[tone]} ${SIZES[size ?? 'md']}`;

  if (!search) {
    return (
      <select className={`${baseStyles} ${look} ${className}`} {...props}>
        {children}
      </select>
    );
  }

  return (
    <SearchableSelect
      search={search}
      selectClassName={`${baseStyles} ${look}`}
      buttonClassName={SEARCH_BUTTON_SIZES[size ?? 'md']}
      className={className}
      selectProps={props}
    >
      {children}
    </SearchableSelect>
  );
}

function SearchableSelect({
  search,
  selectClassName,
  buttonClassName,
  className,
  selectProps,
  children,
}: {
  search: SelectSearch;
  selectClassName: string;
  buttonClassName: string;
  className: string;
  selectProps: Omit<SelectHTMLAttributes<HTMLSelectElement>, 'size' | 'className' | 'children'>;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const boxId = useId();
  const selectRef = useRef<HTMLSelectElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const groupRef = useRef<HTMLDivElement>(null);

  const words = open ? query.toLowerCase().split(/\s+/).filter(Boolean) : [];
  const matched: string[] = [];
  const shown =
    words.length > 0 ? filterOptions(children, words, String(selectProps.value ?? ''), matched) : children;
  const noMatches = words.length > 0 && matched.length === 0;

  const close = (returnFocus: boolean) => {
    setOpen(false);
    setQuery('');
    // The box unmounts, which would leave focus on <body>.
    if (returnFocus) buttonRef.current?.focus();
  };

  // Escape belongs to the search while it is open and the keyboard is anywhere
  // in this field — the box, or the narrowed list below it. On `document`, and
  // stopped there: every modal's Escape is ONE listener on `window`
  // (`modalStack`), so a handler on the box itself would close the search AND
  // the dialog around it.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || !groupRef.current?.contains(document.activeElement)) return;
      event.stopPropagation();
      setOpen(false);
      setQuery('');
      buttonRef.current?.focus();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [open]);

  // Enter takes the first match. The choice goes through the real <select> —
  // its value is set and a native `change` is dispatched — so the caller hears
  // about it exactly as if the option had been picked from the list.
  const pickFirst = () => {
    const select = selectRef.current;
    if (!select || matched.length === 0) return;
    select.value = matched[0];
    select.dispatchEvent(new Event('change', { bubbles: true }));
    close(false);
    select.focus();
  };

  return (
    <div ref={groupRef} className={`flex flex-col gap-1 ${className}`}>
      {open && (
        <input
          id={boxId}
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              pickFirst();
            } else if (e.key === 'ArrowDown') {
              e.preventDefault();
              selectRef.current?.focus();
            }
          }}
          autoFocus
          disabled={selectProps.disabled}
          placeholder={search.placeholder}
          aria-label={search.label}
          className="w-full min-w-0 h-9 min-h-[44px] md:min-h-0 px-3 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary text-white text-sm placeholder:text-bambu-gray focus:outline-none focus:border-bambu-green disabled:opacity-50"
        />
      )}
      {/* Mounted with the box and only its text changes, so a screen reader
          announces the moment a query stops matching. */}
      {open && (
        <p role="status" className={noMatches ? 'text-xs text-bambu-gray' : 'sr-only'}>
          {noMatches ? search.noMatches : ''}
        </p>
      )}
      <div className="flex items-stretch gap-1 min-w-0">
        <select ref={selectRef} className={`flex-1 min-w-0 ${selectClassName}`} {...selectProps}>
          {shown}
        </select>
        <button
          ref={buttonRef}
          type="button"
          onClick={() => (open ? close(false) : setOpen(true))}
          disabled={selectProps.disabled}
          aria-label={search.label}
          aria-expanded={open}
          aria-controls={open ? boxId : undefined}
          title={search.label}
          className={`shrink-0 inline-flex items-center justify-center border border-bambu-dark-tertiary bg-bambu-dark transition-colors hover:text-white focus:outline-none focus:border-bambu-green disabled:opacity-50 disabled:cursor-not-allowed ${
            open ? 'text-bambu-green' : 'text-bambu-gray'
          } ${buttonClassName}`}
        >
          <Search className="w-4 h-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}
