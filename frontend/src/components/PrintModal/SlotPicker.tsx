import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { Check, ChevronDown } from 'lucide-react';
import { FilamentSwatch } from '../FilamentSwatch';
import { displayHex } from '../../utils/displayHex';

/**
 * The slot a file's filament prints from — a list whose every entry shows the
 * spool's colour (upstream #3159, #3294).
 *
 * The control it replaces was a native select: text only, and the text can
 * mislead — a colour's NAME comes from the colour catalogue or, failing that,
 * from its hue, so two different reds are both "Red". Here every slot carries
 * a swatch of what is in it and its hex, and the slot whose colour is exactly
 * the file's is marked.
 *
 * ⚠️ Why not `components/Select.tsx`: that primitive is a native select, and a
 * native option holds text only. This is the one control of that kind in the
 * dialog, so it lives here, beside its two users (FilamentMapping,
 * PrinterSelector), not in the shared layer.
 *
 * ⚠️ The list is NOT portalled. `modalStack` makes everything outside the
 * topmost modal inert, so a list in `body` would be dead under the dialog; it
 * renders inside the dialog's panel, positioned against its own wrapper, and
 * the panel's scroll box simply grows to hold it. Escape closes the list
 * alone: the handler sits on `document` and stops the event before the modal
 * stack's listener on `window` sees it.
 */

export interface SlotOption {
  /** Global tray id. */
  value: number;
  /** Slot label as the printer card shows it ("A1", "HT-A", "Ext"). */
  slot: string;
  /** What is in the slot: the assigned spool's name, else type + colour name. */
  name: string;
  /** Base colour, RRGGBB or RRGGBBAA, with or without '#'. */
  color: string | null | undefined;
  /** The bound spool's further colours and effect, when it has them. */
  extraColors?: string | null;
  effectType?: string | null;
  subtype?: string | null;
  /** Trailing text of the entry's line: grams left, the switch inlet. */
  detail?: string;
  /** The slot's colour is exactly the one the file asks for. */
  exact?: boolean;
  disabled?: boolean;
  disabledReason?: string;
}

interface SlotPickerProps {
  value: number | null;
  options: SlotOption[];
  onChange: (value: number | null) => void;
  /** Shown when nothing is chosen, and as the list's "no slot" entry. */
  placeholder: string;
  /** Border / text colour of the validation state (match, same type, missing). */
  toneClass: string;
  title?: string;
  ariaLabel: string;
  /** Tooltip of the exact-colour mark. */
  exactLabel: string;
}

const rgbaOf = (color: string | null | undefined) => (color ?? '').replace(/^#/, '') || null;

export function SlotPicker({
  value, options, onChange, placeholder, toneClass, title, ariaLabel, exactLabel,
}: SlotPickerProps) {
  const listId = useId();
  const wrapperRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const [open, setOpen] = useState(false);
  // -1 is the "no slot" entry; 0… index `options`.
  const [active, setActive] = useState(-1);
  const [above, setAbove] = useState(false);

  const selectedIndex = options.findIndex((option) => option.value === value);
  const selected = selectedIndex >= 0 ? options[selectedIndex] : null;

  const close = (refocus: boolean) => {
    setOpen(false);
    if (refocus) buttonRef.current?.focus();
  };

  const show = () => {
    setActive(selectedIndex);
    setOpen(true);
  };

  const choose = (index: number) => {
    if (index >= 0 && options[index].disabled) return;
    onChange(index < 0 ? null : options[index].value);
    close(true);
  };

  /** The next enabled entry in a direction, the "no slot" entry included. */
  const step = (from: number, direction: 1 | -1): number => {
    let next = from;
    for (let i = 0; i <= options.length; i += 1) {
      next += direction;
      if (next < -1) next = options.length - 1;
      if (next >= options.length) next = -1;
      if (next < 0 || !options[next].disabled) return next;
    }
    return from;
  };

  // A click anywhere else closes the list; Escape closes it and nothing more.
  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      if (!wrapperRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      event.preventDefault();
      setOpen(false);
      buttonRef.current?.focus();
    };
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  // Open upwards only when the list would run past the bottom of the window
  // AND the whole of it fits above the row inside the dialog's scroll box. A
  // list that overflows downwards just lets that box scroll further; one that
  // overflowed upwards past the box's start could never be scrolled into view.
  useLayoutEffect(() => {
    if (!open || !buttonRef.current || !listRef.current) return;
    const button = buttonRef.current.getBoundingClientRect();
    const needed = listRef.current.offsetHeight;
    const below = window.innerHeight - button.bottom;
    let box: HTMLElement | null = buttonRef.current.parentElement;
    while (box && !/(auto|scroll)/.test(getComputedStyle(box).overflowY)) box = box.parentElement;
    const roomAbove = box ? button.top - box.getBoundingClientRect().top + box.scrollTop : button.top;
    setAbove(needed > below && roomAbove >= needed);
  }, [open, options.length]);

  useEffect(() => {
    if (!open) return;
    listRef.current?.querySelector('[data-active="true"]')?.scrollIntoView?.({ block: 'nearest' });
  }, [open, active]);

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
        event.preventDefault();
        show();
      }
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActive((index) => step(index, 1));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActive((index) => step(index, -1));
    } else if (event.key === 'Home') {
      event.preventDefault();
      setActive(-1);
    } else if (event.key === 'End') {
      event.preventDefault();
      setActive(step(-1, -1));
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      choose(active);
    } else if (event.key === 'Tab') {
      setOpen(false);
    }
  };

  const optionId = (index: number) => `${listId}-${index < 0 ? 'none' : index}`;
  const selectedHex = displayHex(selected?.color);

  return (
    <div ref={wrapperRef} className="relative min-w-0">
      <button
        ref={buttonRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listId}
        aria-activedescendant={open ? optionId(active) : undefined}
        aria-label={ariaLabel}
        title={title}
        data-slot-picker
        data-value={value ?? ''}
        onClick={() => (open ? close(false) : show())}
        onKeyDown={onKeyDown}
        className={`w-full min-h-[28px] flex items-center gap-1.5 px-2 py-1 rounded border text-xs text-left bg-bambu-dark-secondary focus:outline-none focus:ring-1 focus:ring-bambu-green ${toneClass}`}
      >
        {selected ? (
          <>
            <FilamentSwatch
              rgba={rgbaOf(selected.color)}
              extraColors={selected.extraColors}
              effectType={selected.effectType}
              subtype={selected.subtype}
              className="w-3.5 h-3.5"
            />
            <span className="truncate min-w-0 flex-1">{selected.slot}: {selected.name}</span>
            {selectedHex && <span className="font-mono text-[10px] opacity-80 shrink-0">{selectedHex}</span>}
          </>
        ) : (
          <span className="truncate min-w-0 flex-1 text-bambu-gray">{placeholder}</span>
        )}
        <ChevronDown className="w-3 h-3 shrink-0 opacity-70" />
      </button>
      {/* Always in the tree, shown while open: the entries are the control's
          content the way a select's options are. Inside the dialog's panel and
          below the modal layer; on a phone it is exactly as wide as its row,
          wider screens let it grow leftwards to fit its longest entry. */}
      {/* not-a-modal: popover */}
      <ul
        ref={listRef}
        id={listId}
        role="listbox"
        aria-label={ariaLabel}
        hidden={!open}
        className={`absolute right-0 z-20 min-w-full w-max max-w-[min(24rem,calc(100vw-2rem))] max-sm:w-full max-sm:max-w-full max-h-72 overflow-y-auto rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary shadow-xl py-1 ${
          above ? 'bottom-full mb-1' : 'top-full mt-1'
        }`}
      >
          <li
            id={optionId(-1)}
            role="option"
            aria-selected={selected === null}
            data-value=""
            data-active={active === -1}
            onMouseEnter={() => setActive(-1)}
            onClick={() => choose(-1)}
            className={`min-h-[44px] flex items-center px-3 text-xs text-bambu-gray cursor-pointer ${
              active === -1 ? 'bg-bambu-dark-tertiary' : ''
            }`}
          >
            {placeholder}
          </li>
          {options.map((option, index) => {
            const hex = displayHex(option.color);
            return (
              <li
                key={option.value}
                id={optionId(index)}
                role="option"
                aria-selected={option.value === value}
                aria-disabled={option.disabled || undefined}
                data-value={option.value}
                data-active={active === index}
                title={option.disabled ? option.disabledReason : undefined}
                onMouseEnter={() => !option.disabled && setActive(index)}
                onClick={() => choose(index)}
                className={`min-h-[44px] flex items-center gap-2 px-3 py-1.5 text-xs ${
                  option.disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'
                } ${active === index ? 'bg-bambu-dark-tertiary' : ''} ${
                  option.value === value ? 'text-bambu-green' : 'text-white'
                }`}
              >
                <FilamentSwatch
                  rgba={rgbaOf(option.color)}
                  extraColors={option.extraColors}
                  effectType={option.effectType}
                  subtype={option.subtype}
                  className="w-5 h-5"
                />
                <span className="min-w-0 flex-1">
                  <span className="block break-words">
                    {`${option.slot}: ${option.name}${option.detail ?? ''}${
                      option.disabled && option.disabledReason ? ` — ${option.disabledReason}` : ''
                    }`}
                  </span>
                  {hex && <span className="block font-mono text-[10px] text-bambu-gray">{hex}</span>}
                </span>
                {option.exact && (
                  <span title={exactLabel} aria-label={exactLabel} className="shrink-0 text-bambu-green">
                    <Check className="w-4 h-4" />
                  </span>
                )}
              </li>
            );
          })}
      </ul>
    </div>
  );
}
