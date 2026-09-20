import { useEffect, useState, type RefObject } from 'react';

/** How tall a panel is ASSUMED to be when deciding whether to flip it above its
 *  trigger. A guess is enough: being wrong drops the menu a little low on a
 *  short viewport, while measuring would need a second render pass. */
export const ESTIMATED_MENU_HEIGHT = 280;

export interface AnchoredPosition {
  /** Viewport pixels — the panel is `position: fixed`, not laid out. */
  top: number;
  /** Distance from the RIGHT edge of the viewport, so the panel's right edge
   *  lines up with the trigger's however wide the panel is. */
  right: number;
}

/**
 * Where to put a portal-rendered panel that hangs off a trigger.
 *
 * Both "…" menus in this app render into `document.body` — the File Manager's
 * row menu to escape the list's `overflow-hidden`, the grid cards' menu because
 * a card is one big `<Link>` and a `<button>` inside an `<a>` is invalid HTML.
 * Out of the flow, neither can be positioned by layout, so both measure the
 * trigger's own box and hang a fixed panel off it. That arithmetic — right-edge
 * alignment, the 4 px gap, the 8 px viewport margin, the flip above when the
 * bottom is close — was written twice and is one behaviour; a fix applied to
 * one copy is a divergence between two menus that look identical on screen.
 *
 * ⚠️ **`capture` on scroll.** A card grid or a file list scrolls in its own
 * container on some layouts, and a listener on `window` alone never hears that
 * scroll — the panel would sit where the trigger used to be.
 *
 * ⚠️ **`null` until the first measurement**, which is after the panel's first
 * render: the caller must keep it `visibility: hidden` until then, or it
 * flashes at the corner of the screen. Closing clears the coordinates, so a
 * reopen cannot show one frame at wherever the trigger was last time.
 */
export function useAnchoredPosition(
  anchorRef: RefObject<HTMLElement | null>,
  open: boolean,
  estimatedHeight: number = ESTIMATED_MENU_HEIGHT,
): AnchoredPosition | null {
  const [coords, setCoords] = useState<AnchoredPosition | null>(null);

  useEffect(() => {
    if (!open) {
      setCoords(null);
      return;
    }
    const update = () => {
      const el = anchorRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const right = Math.max(8, window.innerWidth - rect.right);
      let top = rect.bottom + 4;
      if (top + estimatedHeight > window.innerHeight - 8 && rect.top > estimatedHeight) {
        top = rect.top - estimatedHeight - 4;
      }
      setCoords({ top, right });
    };
    update();
    window.addEventListener('resize', update);
    window.addEventListener('scroll', update, true);
    return () => {
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', update, true);
    };
  }, [open, anchorRef, estimatedHeight]);

  return coords;
}
