import { useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { MoreVertical } from 'lucide-react';

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

/** How tall the panel is ASSUMED to be when deciding to flip it above the
 *  trigger. A guess is enough: being wrong drops the menu a little low on a
 *  short viewport, while measuring would need a second render pass. */
const ESTIMATED_HEIGHT = 280;

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
 * scroll and resize, the way `FileListActions` does it in the File Manager.
 * It is rendered hidden until the first measurement so it cannot flash at
 * (0, 0).
 */
export function CardActionMenu({ label, testId, width = 180, children }: CardActionMenuProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [coords, setCoords] = useState<{ top: number; right: number } | null>(null);

  useEffect(() => {
    if (!open) {
      setCoords(null);
      return;
    }
    const update = () => {
      const btn = triggerRef.current;
      if (!btn) return;
      const rect = btn.getBoundingClientRect();
      const right = Math.max(8, window.innerWidth - rect.right);
      let top = rect.bottom + 4;
      if (top + ESTIMATED_HEIGHT > window.innerHeight - 8 && rect.top > ESTIMATED_HEIGHT) {
        top = rect.top - ESTIMATED_HEIGHT - 4;
      }
      setCoords({ top, right });
    };
    update();
    // ⚠️ `capture` on scroll: the card grid scrolls in its own container on
    // some layouts, and a listener on `window` alone never hears that one.
    window.addEventListener('resize', update);
    window.addEventListener('scroll', update, true);
    return () => {
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', update, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      // The card grids sit on pages whose modals close on a window keydown —
      // an Escape that shut this menu must not also shut whatever is behind it.
      e.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open]);

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
 *  padding, and so `role="menuitem"` is not something a new entry can forget. */
export function CardActionMenuItem({
  onSelect,
  danger,
  children,
}: {
  onSelect: () => void;
  danger?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onSelect}
      className={`w-full px-3 py-2 text-left text-sm flex items-center gap-2 hover:bg-bambu-dark ${
        danger ? 'text-red-500' : 'text-white'
      }`}
    >
      {children}
    </button>
  );
}
