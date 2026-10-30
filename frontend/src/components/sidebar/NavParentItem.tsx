import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ComponentType, LiHTMLAttributes, ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Link, useLocation } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronUp, GripVertical } from 'lucide-react';
import { useHoverIntent } from '../../hooks/useHoverIntent';
import { useNavOpenState } from '../../hooks/useNavOpenState';
import { useIsAnyModalOpen } from '../modalStack';
import { activeChildId, type NavBadgeKind, type NavChild } from './navChildren';

export interface NavParentItemProps {
  item: { id: string; icon: ComponentType<{ className?: string }>; labelKey: string; children: readonly NavChild[] };
  /** The sidebar shows text (expanded, or the compact drawer); false = the icon rail. */
  expanded: boolean;
  showGrip: boolean;
  badges: Partial<Record<NavBadgeKind, number>>;
  /** Drag handlers and the drop-indicator class from Layout — the parent drags as one entry. */
  liProps: LiHTMLAttributes<HTMLLIElement>;
}

/** The flyout's distance from the window edge when it has to be pushed up. */
const FLYOUT_EDGE_GAP = 8;

function NavCount({ kind, count, onAccent }: { kind: NavBadgeKind; count: number; onAccent: boolean }) {
  const { t } = useTranslation();
  if (count <= 0) return null;
  return (
    <span
      title={t(`nav.badge.${kind}`)}
      className={`min-w-[22px] h-5 px-1.5 inline-flex items-center justify-center rounded-md text-[11px] font-bold tabular-nums ${
        onAccent ? 'bg-white/20 text-white' : 'bg-bambu-green/20 text-bambu-green'
      }`}
    >
      {count > 99 ? '99+' : count}
    </span>
  );
}

/** The children as links; `onNavigate` lets the flyout close itself. */
export function NavChildLinks({
  entries,
  activeId,
  badges,
  onNavigate,
}: {
  entries: readonly NavChild[];
  activeId: string | null;
  badges: Partial<Record<NavBadgeKind, number>>;
  onNavigate?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <>
      {entries.map((child) => {
        const active = child.id === activeId;
        return (
          <li key={child.id}>
            {/* A plain Link: NavLink's own prefix match cannot know our `match`. */}
            <Link
              to={child.to}
              onClick={onNavigate}
              aria-current={active ? 'page' : undefined}
              className={`flex items-center justify-between gap-2 px-3 py-1.5 rounded-lg text-sm transition-colors ${
                active ? 'bg-bambu-green text-white' : 'text-bambu-gray-light hover:bg-bambu-dark-tertiary hover:text-white'
              }`}
            >
              <span>{t(child.labelKey)}</span>
              {child.badge && <NavCount kind={child.badge} count={badges[child.badge] ?? 0} onAccent={active} />}
            </Link>
          </li>
        );
      })}
    </>
  );
}

/**
 * The parent on the collapsed icon rail (spec workshop-nav, rule 4): its
 * children open in a flyout beside the icon.
 * - Hover opens it, and leaving closes it a moment later, so the pointer can
 *   cross the gap.
 * - A click or Enter/Space HOLDS it open: leaving no longer closes it; the
 *   next click does. The icon used to be a link, so people click it — a click
 *   landing on the flyout their hover just opened must keep it, not toggle it
 *   shut. A tap on a touch screen, whose emulated mouseenter lands first, holds
 *   it the same way.
 * - Enter/Space also puts the focus on the first child. A plain Tab onto the
 *   icon does not open it.
 * It is open only on the page it was opened on and never under a modal.
 */
function RailParent({
  item,
  badges,
  liProps,
  activeId,
  hasCount,
  dot,
}: {
  item: NavParentItemProps['item'];
  badges: NavParentItemProps['badges'];
  liProps: NavParentItemProps['liProps'];
  activeId: string | null;
  hasCount: boolean;
  dot: ReactNode;
}) {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const modalOpen = useIsAnyModalOpen();
  // The page the flyout was opened on. A route change or a modal closes it —
  // adjusted during render, React's way of following an input without an effect.
  const [openOn, setOpenOn] = useState<string | null>(null);
  if (openOn !== null && (openOn !== pathname || modalOpen)) setOpenOn(null);
  const open = openOn !== null && openOn === pathname && !modalOpen;

  const iconRef = useRef<HTMLButtonElement>(null);
  const flyoutRef = useRef<HTMLDivElement>(null);
  // Held open by a click or a key rather than by the pointer, and "focus the
  // first child once it is there". Both are written at every opening, so
  // nothing left from an earlier opening reaches the next one.
  const heldRef = useRef(false);
  const focusFirstRef = useRef(false);
  const Icon = item.icon;
  const inside = activeId !== null;

  const close = useCallback(() => {
    heldRef.current = false;
    focusFirstRef.current = false;
    setOpenOn(null);
  }, []);
  const openFlyout = (how: 'hover' | 'click' | 'key') => {
    heldRef.current = how !== 'hover' || (open && heldRef.current);
    focusFirstRef.current = how === 'key';
    setOpenOn(pathname);
  };
  const firstLink = () => flyoutRef.current?.querySelector<HTMLElement>('a') ?? null;
  const hover = useHoverIntent((next: boolean) => {
    if (next) openFlyout('hover');
    else if (!heldRef.current) close();
  });

  // Placed from the icon at the moment it opens, before paint, and pushed up
  // when it would run past the bottom of the window.
  useLayoutEffect(() => {
    const flyout = flyoutRef.current;
    const iconEl = iconRef.current;
    if (!open || !flyout || !iconEl) return;
    const r = iconEl.getBoundingClientRect();
    const lowest = window.innerHeight - flyout.offsetHeight - FLYOUT_EDGE_GAP;
    flyout.style.top = `${Math.max(FLYOUT_EDGE_GAP, Math.min(r.top - 6, lowest))}px`;
    flyout.style.left = `${r.right + 6}px`;
  }, [open]);

  useEffect(() => {
    if (!open) return;
    if (focusFirstRef.current) {
      focusFirstRef.current = false;
      flyoutRef.current?.querySelector<HTMLElement>('a')?.focus();
    }
    // An inner layer's Escape lives on `document` and stops there (modal-stack
    // rule); it is registered only while open, so a closed flyout never eats it.
    // The focus goes back to the icon only if it was in the flyout — a flyout
    // the hover opened must not pull the focus out of a field.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      const hadFocus = !!flyoutRef.current?.contains(document.activeElement);
      close();
      if (hadFocus) iconRef.current?.focus();
    };
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (flyoutRef.current?.contains(target) || iconRef.current?.contains(target)) return;
      close();
    };
    // Placed once, at opening: a scroll anywhere but inside it would leave it
    // hanging away from its icon, so it closes instead — as on a resize.
    const onScroll = (e: Event) => {
      if (flyoutRef.current?.contains(e.target as Node)) return;
      close();
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onDown);
    document.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', close);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', close);
    };
  }, [open, close]);

  return (
    <li {...liProps}>
      {/* No `title`: the flyout that opens on hover already names the section. */}
      <button
        ref={iconRef}
        type="button"
        aria-label={t(item.labelKey)}
        aria-expanded={open}
        data-inside={inside}
        onMouseEnter={hover.enter}
        onMouseLeave={hover.leave}
        onClick={() => (open && heldRef.current ? close() : openFlyout('click'))}
        onKeyDown={(e) => {
          if (e.key !== 'Enter' && e.key !== ' ') return;
          // preventDefault: the button's own activation would fire onClick and toggle it shut again.
          e.preventDefault();
          if (open) {
            heldRef.current = true;
            firstLink()?.focus();
          } else {
            openFlyout('key');
          }
        }}
        // Firefox activates a button on Space's keyup, not its keydown.
        onKeyUp={(e) => {
          if (e.key === ' ') e.preventDefault();
        }}
        className={`relative w-full flex items-center justify-center pl-2 pr-5 py-1.5 rounded-lg transition-colors ${
          inside ? 'bg-bambu-green text-white' : 'text-bambu-gray-light hover:bg-bambu-dark-tertiary hover:text-white'
        }`}
      >
        <span className="relative">
          <Icon className="w-5 h-5 flex-shrink-0" />

        </span>
        <span className="absolute right-1 top-1/2 -translate-y-1/2 flex w-2.5 justify-center" data-nav-attention-slot>{hasCount && dot}</span>
      </button>
      {open &&
        createPortal(
          // Portalled: the sidebar's list scrolls and would clip it. z-[49] keeps it
          // under the modal layer (inv-modals-close-only-by-buttons-or-esc).
          <div
            ref={flyoutRef}
            data-testid={`nav-flyout-${item.id}`}
            style={{ position: 'fixed' }}
            className="z-[49] min-w-[220px] p-1.5 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-xl"
            onMouseEnter={hover.enter}
            onMouseLeave={hover.leave}
            onKeyDown={(e) => {
              // The flyout sits at the end of <body>: out of it means back into
              // the rail, through the icon. Shift+Tab stops ON the icon; Tab lets
              // the browser move on from it to the next entry.
              if (e.key !== 'Tab') return;
              const links = flyoutRef.current?.querySelectorAll<HTMLElement>('a');
              if (!links?.length) return;
              if (e.target !== (e.shiftKey ? links[0] : links[links.length - 1])) return;
              if (e.shiftKey) e.preventDefault();
              iconRef.current?.focus();
              close();
            }}
            onBlur={(e) => {
              // Only a focus that moved to a real element outside closes it: a click
              // on the non-focusable heading blurs a link with no relatedTarget.
              const next = e.relatedTarget as Node | null;
              if (!next || flyoutRef.current?.contains(next) || iconRef.current?.contains(next)) return;
              close();
            }}
          >
            <nav aria-label={t(item.labelKey)}>
              <div className="px-3 pt-1 pb-1.5 text-[10px] uppercase tracking-wider text-bambu-gray font-medium">
                {t(item.labelKey)}
              </div>
              <ul className="space-y-0.5">
                <NavChildLinks entries={item.children} activeId={activeId} badges={badges} onNavigate={close} />
              </ul>
            </nav>
          </div>,
          document.body,
        )}
    </li>
  );
}

/**
 * A sidebar entry with children (spec workshop-nav, rules 1–5). Expanded: a
 * toggle over its children on a dashed guide line. Rail: an icon button whose
 * children open in a flyout. The parent drags as one entry (`liProps`).
 */
export function NavParentItem({ item, expanded, showGrip, badges, liProps }: NavParentItemProps) {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const [open, setOpen] = useNavOpenState(item.id);
  const activeId = activeChildId(item.children, pathname);
  const inside = activeId !== null;
  const hasCount = item.children.some((child) => child.badge && (badges[child.badge] ?? 0) > 0);
  const Icon = item.icon;
  const dot: ReactNode = (
    <span
      data-testid={`nav-dot-${item.id}`}
      role="img" aria-label={t(item.labelKey)} title={t(item.labelKey)}
      className="w-2.5 h-2.5 bg-bambu-green rounded-full"
    />
  );

  if (!expanded) {
    return <RailParent item={item} badges={badges} liProps={liProps} activeId={activeId} hasCount={hasCount} dot={dot} />;
  }

  const folded = !open;
  return (
    <li {...liProps}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-label={t(item.labelKey)}
        aria-expanded={open}
        data-inside={inside}
        className={`relative w-full flex items-center gap-3 pl-4 pr-9 py-1.5 rounded-lg transition-colors group ${
          folded && inside
            ? 'bg-bambu-green text-white'
            : inside
              ? 'text-white hover:bg-bambu-dark-tertiary'
              : 'text-bambu-gray-light hover:bg-bambu-dark-tertiary hover:text-white'
        }`}
      >
        {showGrip && (
          <GripVertical className="w-4 h-4 flex-shrink-0 opacity-0 group-hover:opacity-50 cursor-grab active:cursor-grabbing -ml-1" />
        )}
        <span className="relative">
          <Icon className="w-5 h-5 flex-shrink-0" />

        </span>
        <span className="min-w-0 flex-1 truncate text-left">{t(item.labelKey)}</span>
        {open ? <ChevronUp className="w-4 h-4 shrink-0" aria-hidden /> : <ChevronDown className="w-4 h-4 shrink-0" aria-hidden />}
        <span className="absolute right-1 top-1/2 -translate-y-1/2 flex w-2.5 justify-center" data-nav-attention-slot>{folded && hasCount && dot}</span>
      </button>
      {open && (
        <ul className="mt-1 mb-0.5 ml-[1.6rem] pl-3 border-l border-dashed border-bambu-gray/40 space-y-0.5">
          <NavChildLinks entries={item.children} activeId={activeId} badges={badges} />
        </ul>
      )}
    </li>
  );
}
