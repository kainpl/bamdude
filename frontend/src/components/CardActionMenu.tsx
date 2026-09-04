import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { MoreVertical } from 'lucide-react';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';

interface CardActionMenuProps {
  /** Accessible name of the trigger — the cards all say `common.actions`. */
  label: string;
  /** Optional hook for tests that open the menu by id rather than by name. */
  testId?: string;
  /** Menu width in px. Fixed, because the panel is positioned, not laid out. */
  width?: number;
  /** The items. Called with `close` so each item shuts the menu itself — the
   *  panel cannot close on a bubbling click, being in another tree. */
  children: (close: () => void) => ReactNode;
}

/**
 * The "…" menu of a grid card, rendered in a portal on `document.body`.
 *
 * ⚠️ **The portal is not about clipping, it is about the anchor.** A card is
 * one big `<Link>`, and a `<button>` inside an `<a>` is invalid HTML that no
 * two browsers agree about: the menu used to live in the anchor and cancel the
 * navigation it caused with `preventDefault` + `stopPropagation` on every
 * single item. That worked for a mouse and not for a keyboard, and one item
 * added without the guard navigated instead of acting. Out of the anchor there
 * is nothing to cancel.
 *
 * The panel is `position: fixed` from the trigger's own box and recomputed on
 * scroll and resize — the arithmetic is `useAnchoredPosition`, shared with the
 * File Manager's row menu, which does the same thing for the same reason. It is
 * rendered hidden until the first measurement so it cannot flash at (0, 0).
 *
 * ⚠️ **A `role="menu"` is a keyboard widget, not a styled div.** Opening moves
 * focus to the first item, Up/Down walk the items (wrapping), Home/End jump to
 * the ends, and Escape closes and gives the focus back to the trigger. Without
 * the move, opening with the keyboard left the focus on the trigger and Tab
 * walked into the page BEHIND the panel — the menu was announced and then
 * unreachable.
 */
export function CardActionMenu({ label, testId, width = 180, children }: CardActionMenuProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const coords = useAnchoredPosition(triggerRef, open);

  /** The items, in DOM order, as the roving keys see them. Read on every press
   *  rather than kept in state: what a card offers depends on permissions and
   *  on the row, and a stale list would move focus to an item that is gone.
   *
   *  ⚠️ **Disabled items are not in the ring.** A disabled `<button>` cannot
   *  take focus at all, so leaving it in would make Down stop dead on it and the
   *  opening focus land nowhere when the first entry happens to be the busy
   *  one. */
  const items = useCallback(
    () => Array.from(panelRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not(:disabled)') ?? []),
    [],
  );

  // ⚠️ The move waits for the panel to be MEASURED (focusing an element that is
  // still `visibility: hidden` is a no-op in a browser, and the menu would open
  // with the focus left behind on the trigger) and happens ONCE per opening:
  // the coordinates are recomputed on every scroll and resize, so a plain
  // effect on them would yank the focus back to the first entry while somebody
  // was arrowing through the menu on a scrolling page.
  const moved = useRef(false);
  useEffect(() => {
    if (!open) {
      moved.current = false;
      return;
    }
    if (coords && !moved.current) {
      moved.current = true;
      items()[0]?.focus();
    }
  }, [open, coords, items]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        // ⚠️ **`stopPropagation` could not do this job.** The overlays this menu
        // opens inside listen on `window` too, and `stopPropagation` stops an
        // event travelling to OTHER targets — it says nothing about the other
        // listeners already registered on the same one. Worse, ours is
        // registered LAST (on opening, long after the dialog mounted), so by
        // the time it ran the dialog's had already closed the dialog. Hence
        // both halves below: the capture phase to be heard FIRST, and
        // `stopImmediatePropagation` to be heard alone.
        e.stopImmediatePropagation();
        setOpen(false);
        triggerRef.current?.focus();
        return;
      }
      const rows = items();
      if (rows.length === 0) return;
      const at = rows.indexOf(document.activeElement as HTMLElement);
      // Wrapping, because a menu is a ring: Down on the last entry is the first
      // one, which is what every desktop menu does and what a screen reader's
      // own menu mode expects.
      const go = (index: number) => {
        e.preventDefault();
        e.stopPropagation();
        rows[(index + rows.length) % rows.length]?.focus();
      };
      if (e.key === 'ArrowDown') go(at + 1);
      else if (e.key === 'ArrowUp') go(at <= 0 ? rows.length - 1 : at - 1);
      else if (e.key === 'Home') go(0);
      else if (e.key === 'End') go(rows.length - 1);
    };
    // Capture, so an open menu is the FIRST thing the page's keys reach — see
    // the Escape branch. Registered and torn down with the menu, so nothing
    // outside an open menu is intercepted at all.
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [open, items]);

  const close = () => setOpen(false);

  return (
    <>
      {/* ⚠️ `haspopup` + `expanded` on the TRIGGER: the panel carries
          `role="menu"`, but a screen reader meets this button first and without
          these it is announced as an ordinary button — nothing says a menu
          opens, or that one is already open. */}
      <button
        ref={triggerRef}
        type="button"
        data-testid={testId}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        onClick={() => setOpen((v) => !v)}
        className="p-1.5 rounded-lg hover:bg-bambu-dark text-bambu-gray hover:text-white transition-colors"
      >
        <MoreVertical className="w-4 h-4" />
      </button>
      {open
        && createPortal(
          <>
            <div className="fixed inset-0 z-[55]" onClick={close} />
            <div
              ref={panelRef}
              role="menu"
              data-testid={testId ? `${testId}-panel` : undefined}
              style={{
                position: 'fixed',
                top: coords?.top ?? 0,
                right: coords?.right ?? 0,
                width,
                visibility: coords ? 'visible' : 'hidden',
              }}
              className="z-[60] bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl py-1 whitespace-nowrap"
            >
              {children(close)}
            </div>
          </>,
          document.body,
        )}
    </>
  );
}

/** One row of a card menu. Shared so the two cards cannot drift apart on
 *  padding, and so `role="menuitem"` is not something a new entry can forget.
 *
 *  `disabled` is the native attribute, deliberately: it stops the click AND
 *  takes the row out of the roving ring above, which is what an item that is
 *  mid-request should do. A hand-rolled menu row that only dimmed itself still
 *  fired on a second click. */
export function CardActionMenuItem({
  onSelect,
  danger,
  disabled,
  children,
}: {
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onSelect}
      disabled={disabled}
      className={`w-full px-3 py-2 text-left text-sm flex items-center gap-2 hover:bg-bambu-dark disabled:opacity-50 disabled:hover:bg-transparent ${
        danger ? 'text-red-500' : 'text-white'
      }`}
    >
      {children}
    </button>
  );
}
