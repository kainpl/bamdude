/**
 * A dropdown whose rows are the cards they replaced.
 *
 * ⚠️ **Not a `<select>`.** A native option is one line of plain text, and the
 * three things that tell two labels apart — the name, what it is for, and how
 * big it is — do not fit on one. The print dialog used to lay these out as a
 * grid of cards, which read well and did not scale: every design anybody drew
 * added another card to scroll past before reaching the buttons underneath.
 * This keeps the card and folds the list away.
 *
 * ⚠️ **The panel is a portal, not an absolutely-positioned child.** Its only
 * home is inside a scrolling dialog, and a panel positioned within one is
 * clipped by it — the list opened and almost none of it was visible. Escaping
 * to the body means the position has to be measured and re-measured instead,
 * which is the price of not being inside the box.
 *
 * ⚠️ **A row that cannot be used is shown and disabled, never hidden.** The
 * reason is the whole value: "this one is 60 × 40 and your printer has 50 × 30
 * loaded" tells somebody to change the roll. Dropping it silently tells them
 * their design vanished.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown } from 'lucide-react';

export interface CardOption {
  /** ``null`` is the "let something else decide" row, where one is offered. */
  id: number | null;
  title: string;
  description?: string | null;
  /** The short right-hand fact — a size, a grid. */
  meta?: string | null;
  /** Why this cannot be chosen right now. Replaces the description when set. */
  complaint?: string | null;
}

interface CardSelectProps {
  label: string;
  options: CardOption[];
  value: number | null;
  onChange: (id: number | null) => void;
  disabled?: boolean;
}

const CARD =
  'w-full text-left p-2.5 rounded-lg border bg-bambu-dark flex items-center gap-2 disabled:opacity-50';

/** Tallest the panel gets, the gap it keeps from the trigger, the margin it
 *  keeps from the bottom of the window, and the height below which shrinking it
 *  further stops helping and it should just scroll. */
const PANEL_MAX_PX = 288;
const GAP_PX = 4;
const EDGE_PX = 8;
const MIN_PANEL_PX = 140;

function Body({ option, chosen }: { option: CardOption; chosen?: boolean }) {
  const complaint = option.complaint ?? null;
  return (
    <>
      {chosen !== undefined && (
        <Check className={`w-4 h-4 shrink-0 ${chosen ? 'text-bambu-green' : 'opacity-0'}`} />
      )}
      <span className="flex-1 min-w-0">
        <span className="block font-medium text-white text-sm truncate">{option.title}</span>
        {(complaint ?? option.description) && (
          <span
            className={`block text-xs mt-0.5 truncate ${
              complaint ? 'text-amber-600 dark:text-amber-400' : 'text-bambu-gray'
            }`}
          >
            {complaint ?? option.description}
          </span>
        )}
      </span>
      {option.meta && <span className="text-xs text-bambu-gray shrink-0">{option.meta}</span>}
    </>
  );
}

export function CardSelect({ label, options, value, onChange, disabled }: CardSelectProps) {
  const [open, setOpen] = useState(false);
  const [box, setBox] = useState<{ left: number; top: number; width: number; maxHeight: number } | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLUListElement>(null);

  /** Directly under the trigger. Always under it.
   *
   *  ⚠️ There is deliberately no flip-upward. It was here, and the two controls
   *  sit close enough together that one opened down and its neighbour opened up
   *  — the same gesture producing opposite motion, which is the kind of thing
   *  you feel as wrong before you can name it. When the room below is short the
   *  list scrolls inside itself instead; that costs a scroll, and it costs it
   *  predictably. */
  const measure = useCallback(() => {
    const rect = trigger.current?.getBoundingClientRect();
    if (!rect) return;
    const below = window.innerHeight - rect.bottom - GAP_PX - EDGE_PX;
    setBox({
      left: rect.left,
      top: rect.bottom + GAP_PX,
      width: rect.width,
      maxHeight: Math.max(MIN_PANEL_PX, Math.min(PANEL_MAX_PX, below)),
    });
  }, []);

  useLayoutEffect(() => {
    if (open) measure();
  }, [open, measure]);

  useEffect(() => {
    if (!open) return;

    // ⚠️ The containment check has to include the PANEL, which is no longer a
    // descendant of the trigger. Without it, mousedown inside the panel closes
    // the list and the click that follows lands on nothing at all.
    const onDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (trigger.current?.contains(target) || panel.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    // Capture: a scroll inside the dialog does not bubble to the window, and
    // that is exactly the scroll that moves the trigger out from under us.
    const onScroll = () => measure();

    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onScroll);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onScroll);
    };
  }, [open, measure]);

  const selected = options.find((option) => option.id === value) ?? options[0] ?? null;
  if (!selected) return null;

  const list = (
    <ul
      ref={panel}
      role="listbox"
      aria-label={label}
      style={{
        position: 'fixed',
        left: box?.left ?? 0,
        top: box?.top ?? 0,
        width: box?.width ?? 'auto',
        maxHeight: box?.maxHeight ?? PANEL_MAX_PX,
        // Above the dialog, which sits at z-50 with its own backdrop.
        zIndex: 1000,
      }}
      className="overflow-y-auto p-1 space-y-1 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary shadow-xl"
    >
      {options.map((option) => {
        const chosen = option.id === selected.id;
        return (
          <li key={String(option.id)}>
            <button
              type="button"
              role="option"
              aria-selected={chosen}
              disabled={option.complaint != null}
              title={option.complaint ?? undefined}
              onClick={() => {
                onChange(option.id);
                setOpen(false);
              }}
              className={`${CARD} ${
                chosen ? 'border-bambu-green' : 'border-bambu-dark-tertiary hover:border-bambu-green'
              }`}
            >
              <Body option={option} chosen={chosen} />
            </button>
          </li>
        );
      })}
    </ul>
  );

  return (
    <>
      <button
        ref={trigger}
        type="button"
        role="combobox"
        aria-label={label}
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((was) => !was)}
        className={`${CARD} border-bambu-dark-tertiary hover:border-bambu-green`}
      >
        <Body option={selected} />
        <ChevronDown className="w-4 h-4 text-bambu-gray shrink-0" />
      </button>

      {open && createPortal(list, document.body)}
    </>
  );
}
