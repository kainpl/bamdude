import { useState, useRef, useEffect, useLayoutEffect, useCallback, useId, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Droplets, Copy, Check, Settings2, Package, Repeat, Unlink } from 'lucide-react';
import { isLightColor, resolveSpoolColorName } from '../utils/colors';
import { buildFilamentBackground, parseStops } from './filamentSwatchHelpers';
import { Modal } from './Modal';

type CardPlacement = { top: number; left: number; side: 'top' | 'bottom'; arrowLeft: number; maxHeight: number };

/** Where a hover card goes, in DOCUMENT coordinates.
 *
 * ⚠️ The card is portalled to `document.body`: as a child of the trigger it
 * was clipped by the printer card it lives in. The horizontal clamp keeps it
 * off the fixed sidebar; it is the card's actual `left` rather than a
 * `marginLeft` correction layered on top of `-translate-x-1/2`.
 *
 * ⚠️ `absolute` in DOCUMENT coordinates (viewport + scroll), placed once when
 * the card opens — NOT `fixed` re-placed from a scroll listener. The page
 * scrolls on the window, so the browser carries an absolute card with its slot
 * the way it carries the bed-jog menu; a fixed card chasing the slot from a
 * scroll listener visibly floated over the page. Re-placed on resize only,
 * when the printer card itself reflows.
 *
 * ⚠️ `useLayoutEffect`, not `useEffect`. Measuring needs the card in the DOM,
 * and an unmeasured first frame paints at the origin — a flash in the corner.
 * Running before paint is what keeps the port invisible.
 */
function useCardPlacement(
  isVisible: boolean,
  triggerRef: React.RefObject<HTMLDivElement | null>,
  cardRef: React.RefObject<HTMLDivElement | null>,
): CardPlacement | null {
  const [placement, setPlacement] = useState<CardPlacement | null>(null);

  useLayoutEffect(() => {
    if (!isVisible) {
      setPlacement(null);
      return;
    }
    const measure = () => {
      const trigger = triggerRef.current;
      const card = cardRef.current;
      if (!trigger || !card) return;

      const rect = trigger.getBoundingClientRect();
      const cardWidth = card.offsetWidth;
      const naturalHeight = Math.max(card.offsetHeight, card.firstElementChild?.scrollHeight ?? 0);
      // The app header is fixed at 56px; space above the trigger excludes it.
      const headerHeight = 56;
      const gap = 8;
      const safeMargin = 8;

      const spaceAbove = rect.top - headerHeight - gap - safeMargin;
      const spaceBelow = window.innerHeight - rect.bottom - gap - safeMargin;
      const side = naturalHeight > spaceAbove && spaceBelow > spaceAbove ? 'bottom' : 'top';
      const maxHeight = Math.max(0, side === 'top' ? spaceAbove : spaceBelow);
      const cardHeight = Math.min(maxHeight, naturalHeight);

      // Anchor the safe-left edge on `<main>` so the threshold tracks the
      // sidebar's expanded / collapsed / hidden state on its own.
      const main = document.querySelector('main');
      const triggerCenterX = rect.left + rect.width / 2;
      const minLeft = (main ? main.getBoundingClientRect().left : 0) + safeMargin;
      const maxLeft = window.innerWidth - cardWidth - safeMargin;
      const left = Math.max(safeMargin, Math.min(Math.max(minLeft, triggerCenterX - cardWidth / 2), maxLeft));

      setPlacement({
        top: (side === 'top' ? rect.top - cardHeight - gap : rect.bottom + gap) + window.scrollY,
        left: left + window.scrollX,
        side,
        maxHeight,
        // Arrow keeps pointing at the trigger after the card was clamped
        // (both in viewport terms, so the scroll offset cancels out).
        arrowLeft: triggerCenterX - left,
      });
    };

    measure();
    window.addEventListener('resize', measure);
    return () => {
      window.removeEventListener('resize', measure);
    };
  }, [isVisible, triggerRef, cardRef]);

  return placement;
}

/**
 * A mouse may hover a slot while touch, pen and keyboard open it explicitly.
 * The card is portalled, so its pointer and focus ranges include both roots.
 */
function useSlotCard(disabled = false) {
  const [mode, setMode] = useState<'hover' | 'open' | null>(null);
  const isVisible = !disabled && mode !== null;
  const cardId = useId();
  const triggerRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pressType = useRef('');
  const focusCard = useRef(false);

  const clearTimer = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }, []);
  const dismiss = useCallback(() => {
    clearTimer();
    focusCard.current = false;
    setMode(null);
  }, [clearTimer]);

  useEffect(() => clearTimer, [clearTimer]);
  useEffect(() => {
    if (disabled) dismiss();
  }, [disabled, dismiss]);

  useEffect(() => {
    if (!isVisible) return;
    const contains = (node: EventTarget | null) =>
      node instanceof Node && (triggerRef.current?.contains(node) || cardRef.current?.contains(node));
    const dismissOutside = (event: Event) => {
      if (!contains(event.target)) dismiss();
    };
    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      const restoreFocus = contains(document.activeElement);
      dismiss();
      if (restoreFocus) {
        event.preventDefault();
        triggerRef.current?.focus();
      }
    };
    document.addEventListener('pointerdown', dismissOutside, true);
    document.addEventListener('focusin', dismissOutside);
    document.addEventListener('keydown', onEscape);
    return () => {
      document.removeEventListener('pointerdown', dismissOutside, true);
      document.removeEventListener('focusin', dismissOutside);
      document.removeEventListener('keydown', onEscape);
    };
  }, [isVisible, dismiss]);

  useEffect(() => {
    if (!isVisible || mode !== 'open' || !focusCard.current) return;
    const frame = requestAnimationFrame(() => {
      focusCard.current = false;
      const card = cardRef.current;
      (card?.querySelector<HTMLElement>('button:not([disabled]), a[href], [tabindex="0"]') ?? card)?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [isVisible, mode]);

  const hoverProps = {
    onPointerEnter: (event: React.PointerEvent<HTMLDivElement>) => {
      if (disabled || event.pointerType !== 'mouse' || mode === 'open') return;
      clearTimer();
      if (mode === null) timer.current = setTimeout(() => setMode('hover'), 80);
    },
    onPointerLeave: (event: React.PointerEvent<HTMLDivElement>) => {
      if (event.pointerType !== 'mouse' || mode === 'open') return;
      clearTimer();
      if (mode === 'hover') timer.current = setTimeout(dismiss, 100);
    },
  };

  function activate(focus: boolean) {
    clearTimer();
    if (mode === 'open') {
      dismiss();
    } else {
      focusCard.current = focus;
      setMode('open');
    }
  }

  const triggerProps = {
    ...hoverProps,
    ref: triggerRef,
    role: disabled ? undefined : 'button',
    tabIndex: disabled ? undefined : 0,
    'aria-haspopup': disabled ? undefined : ('dialog' as const),
    'aria-expanded': disabled ? undefined : isVisible,
    'aria-controls': isVisible ? cardId : undefined,
    'data-ams-slot-trigger': disabled ? undefined : '',
    onPointerDown: (event: React.PointerEvent<HTMLDivElement>) => {
      if (event.currentTarget.contains(event.target as Node)) pressType.current = event.pointerType;
    },
    onClick: (event: React.MouseEvent<HTMLDivElement>) => {
      // React bubbles portal events through this wrapper even when the DOM does not.
      if (disabled || !event.currentTarget.contains(event.target as Node)) return;
      const pointer = pressType.current;
      pressType.current = '';
      if (pointer === 'mouse' && mode === 'hover') return;
      activate(event.detail === 0 && !pointer);
    },
    onKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (disabled || event.target !== event.currentTarget || (event.key !== 'Enter' && event.key !== ' ')) return;
      event.preventDefault();
      if (!event.repeat) activate(true);
    },
  };
  return { isVisible, triggerRef, cardRef, cardId, triggerProps, hoverProps, dismiss };
}

interface FilamentData {
  vendor: 'Bambu Lab' | 'Generic';
  profile: string;
  /** Catalogue name for the loaded colour. Callers that know the material
   *  resolve it with ``getColorName(hex, tray_sub_brands)`` -- a white Matte
   *  spool is Ivory White, not the Jade White that shares its hex (upstream
   *  #2875). An assigned spool overrides this; see ``displayColorName``. */
  colorName: string;
  colorHex: string | null;
  kFactor: string;
  fillLevel: number | null; // null = unknown
  trayUuid?: string | null; // Bambu Lab spool UUID for Spoolman linking
  tagUid?: string | null; // Generic NFC tag UID fallback for linking
  fillSource?: 'ams' | 'spoolman' | 'inventory'; // Source of fill level data
  // What the printer was TOLD this slot holds (backup-compatibility emulation).
  // Non-null only while the advertised profile differs from the real spool —
  // everything else on this card describes the spool itself.
  advertised?: { colorHex: string | null; profile: string | null } | null;
}

interface SpoolmanConfig {
  enabled: boolean;
  onLinkSpool?: () => void;
  onUnlinkSpool?: () => void;
  linkedSpoolId?: number | null; // Spoolman spool ID if this tray is already linked
  spoolmanUrl?: string | null; // Base URL for Spoolman (for "Open in Spoolman" link)
  syncMode?: string | null; // If auto-sync is enabled, we may want to hide the unlink option for Bambu spools
}

interface InventoryConfig {
  onAssignSpool?: () => void;
  onUnassignSpool?: () => void;
  assignedSpool?: {
    id: number;
    material: string;
    brand: string | null;
    color_name: string | null;
    // The spool's own swatch. A tray reports one `tray_color` hex and can
    // never describe a gradient or a surface effect, so a tri-colour roll
    // read as one flat band (upstream #2967).
    subtype?: string | null;
    rgba?: string | null;
    extra_colors?: string | null;
    effect_type?: string | null;
    remainingWeightGrams?: number | null;
    /**
     * Pre-formatted display name from the user's spool-name template
     * (`Settings → Inventory → Spool display name`). Required — every caller
     * must route the spool through ``formatSpoolDisplayName`` so the hover card
     * shows the same string the operator sees in the inventory list and the
     * assign-spool picker, never an ad-hoc brand/material/colour concatenation.
     */
    displayName: string;
  } | null;
  isAssigned?: boolean;
  /**
   * Why no inventory can take an assignment for this slot right now (Spoolman
   * mode with Spoolman unreachable). Assign is then shown disabled with this
   * text — a control that vanishes reads as a permissions problem.
   */
  unavailableReason?: string;
}

interface ConfigureSlotConfig {
  enabled: boolean;
  onConfigure?: () => void;
}

interface FilamentHoverCardProps {
  data: FilamentData;
  children: ReactNode;
  disabled?: boolean;
  className?: string;
  spoolman?: SpoolmanConfig;
  inventory?: InventoryConfig;
  configureSlot?: ConfigureSlotConfig;
}

/**
 * A hover card that displays filament details when hovering over AMS slots.
 * Replaces the basic browser tooltip with a styled popover.
 */
export function FilamentHoverCard({ data, children, disabled, className = '', spoolman, inventory, configureSlot }: FilamentHoverCardProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { isVisible, triggerRef, cardRef, cardId, triggerProps, hoverProps, dismiss } = useSlotCard(disabled);
  const [copied, setCopied] = useState(false);
  const [showUnlinkConfirm, setShowUnlinkConfirm] = useState(false);
  const placement = useCardPlacement(isVisible, triggerRef, cardRef);

  const handleCopyUuid = () => {
    const uuid = data.trayUuid;
    if (!uuid) return;

    // Try modern clipboard API first, fallback to execCommand
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(uuid).then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      }).catch(() => {
        // Fallback on error
        fallbackCopy(uuid);
      });
    } else {
      fallbackCopy(uuid);
    }
  };

  const fallbackCopy = (text: string) => {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    try {
      document.execCommand('copy');
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      console.error('Failed to copy to clipboard');
    }
    document.body.removeChild(textarea);
  };


  // Get fill bar color based on percentage
  const getFillColor = (fill: number): string => {
    if (fill <= 15) return '#ef4444'; // red
    if (fill <= 30) return '#f97316'; // orange
    if (fill <= 50) return '#eab308'; // yellow
    return '#22c55e'; // green
  };

  const colorHex = data.colorHex ? `#${data.colorHex.replace('#', '')}` : null;

  // An assigned spool outranks any hex lookup: it is the roll the user put in
  // this slot, named by whoever created it, and it is already shown two rows
  // below under ASSIGNED. Passing a null rgba keeps the helper to its
  // readable-name test -- a Bambu internal code like "A06-D0" is not a name
  // (#857) and must not displace the catalogue answer, which the caller has
  // already resolved with the slot's material (#2875).
  // Trimmed, so a spool saved with a whitespace-only colour name leaves the
  // swatch reading the catalogue answer instead of reading blank.
  const assignedColorName = resolveSpoolColorName(inventory?.assignedSpool?.color_name ?? null, null)?.trim();
  const displayColorName = assignedColorName || data.colorName;
  const assignedRemainingWeight = inventory?.assignedSpool?.remainingWeightGrams ?? null;

  // The header paints the assigned spool's own swatch whenever the spool says
  // more than one colour or a surface effect (upstream #2967), through the
  // builder the Inventory swatches use, so the two cannot disagree. A plain
  // single-colour spool keeps the flat slot colour. "Any stop at all", not
  // "more than one": the builder ignores rgba once stops exist, so a one-stop
  // spool paints that stop — as its Inventory row does.
  const assignedSwatch = inventory?.assignedSpool ?? null;
  const swatchStops = parseStops(assignedSwatch?.extra_colors);
  const useSpoolSwatch = Boolean(assignedSwatch) && (swatchStops.length > 0 || Boolean(assignedSwatch?.effect_type));
  const spoolSwatchStyle = useSpoolSwatch
    ? buildFilamentBackground({
        rgba: assignedSwatch?.rgba ?? colorHex,
        extraColors: assignedSwatch?.extra_colors,
        effectType: assignedSwatch?.effect_type,
        subtype: assignedSwatch?.subtype,
      })
    : null;
  // Several bands: no single hex decides legibility, so the name goes on the
  // scrim the vendor badge already uses. One stop, or an effect over one
  // colour, keeps the contrast test — against the colour actually painted.
  const swatchNeedsScrim = swatchStops.length > 1;
  const contrastBaseHex = useSpoolSwatch
    ? (swatchStops.length === 1 ? swatchStops[0] : assignedSwatch?.rgba ?? colorHex)
    : colorHex;

  return (
    <div
      {...triggerProps}
      className={`relative ${className}`}
    >
      {children}

      {/* Hover Card — portalled out of the trigger, see useCardPlacement. */}
      {isVisible && createPortal(
        <div
          ref={cardRef}
          {...hoverProps}
          id={cardId}
          role="dialog"
          aria-label={t('ams.slotDetails')}
          tabIndex={-1}
          data-ams-slot-card=""
          data-testid="filament-hover-card"
          className="absolute z-[60] animate-in fade-in-0 zoom-in-95"
          style={{
            // ⚠️ Never transition the position. A bare `duration-*` class sets
            // `transition-duration` while `transition-property` stays at its
            // initial `all`, so `top`/`left` animated from the viewport origin
            // and the card visibly flew in from the top-left corner. Harmless
            // while the position came from static classes; not harmless now
            // that it is measured and assigned. The enter animation is a
            // keyframe (`animate-in`), which is unaffected.
            transition: 'none',
            top: placement?.top ?? 0,
            left: placement?.left ?? 0,
            // Hidden until measured: an unmeasured frame paints in the corner.
            visibility: placement ? 'visible' : 'hidden',
            maxWidth: 'calc(100vw - 16px)',
          }}
        >
          {/* Card container */}
          <div className="
            w-52 max-w-full bg-bambu-dark-secondary border border-bambu-dark-tertiary
            rounded-lg shadow-xl overflow-y-auto backdrop-blur-sm
          " style={{ maxHeight: placement?.maxHeight }}>
            {/* Color swatch header - the hero element */}
            <div
              className="h-12 relative overflow-hidden"
              style={
                spoolSwatchStyle
                  ? { ...spoolSwatchStyle, backgroundColor: colorHex || '#3d3d3d' }
                  : { backgroundColor: colorHex || '#3d3d3d' }
              }
            >
              {/* Subtle gradient overlay for depth */}
              <div className="absolute inset-0 bg-gradient-to-b from-white/10 to-transparent" />

              {/* Color name on swatch */}
              <div className="absolute inset-0 flex items-center justify-center">
                <span
                  className={
                    swatchNeedsScrim
                      ? 'px-2 py-0.5 rounded bg-black/60 text-white font-semibold text-sm tracking-wide'
                      : `font-semibold text-sm tracking-wide ${
                          isLightColor(contrastBaseHex) ? 'text-black/80' : 'text-white/90'
                        }`
                  }
                >
                  {displayColorName}
                </span>
              </div>

              {/* Vendor badge - solid background for visibility on any color */}
              <div className={`
                absolute top-1.5 right-1.5 px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider
                ${data.vendor === 'Bambu Lab'
                  ? 'bg-black/60 text-white'
                  : 'bg-black/50 text-white/90'}
              `}>
                {data.vendor === 'Bambu Lab' ? 'BBL' : 'GEN'}
              </div>
            </div>

            {/* Details section */}
            <div className="p-3 space-y-2.5">
              {/* Profile name */}
              <div className="flex items-center justify-between">
                <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">
                  {t('ams.profile')}
                </span>
                <span className="text-xs text-white font-semibold truncate max-w-[120px]">
                  {data.profile}
                </span>
              </div>

              {/* What the printer was told instead, so the AMS groups this slot
                  with its peers for auto-refill (backup-compatibility policy). */}
              {data.advertised && (
                <div className="mt-1 flex items-center gap-1.5 text-[11px]" style={{ color: 'var(--text-secondary)' }}>
                  <span
                    className="inline-block w-2.5 h-2.5 rounded-full border"
                    style={{ backgroundColor: data.advertised.colorHex ? `#${data.advertised.colorHex.replace('#', '').slice(0, 6)}` : 'transparent' }}
                  />
                  <span>{t('printers.amsCompat.badge', { profile: data.advertised.profile || '—' })}</span>
                </div>
              )}

              {/* K Factor */}
              <div className="flex items-center justify-between">
                <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">
                  {t('ams.kFactor')}
                </span>
                <span className="text-xs text-bambu-green font-mono font-bold">
                  {data.kFactor}
                </span>
              </div>

              {/* Fill Level */}
              <div className="space-y-1">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium flex items-center gap-1">
                    <Droplets className="w-3 h-3" />
                    {t('ams.fill')}
                  </span>
                  <span className="text-xs text-white font-semibold flex items-center gap-1">
                    <span>{data.fillLevel !== null ? `${data.fillLevel}%` : '-'}</span>
                    {data.fillSource === 'spoolman' && data.fillLevel !== null && (
                      <span className="text-[9px] text-bambu-gray font-normal">{t('spoolman.fillSourceLabel')}</span>
                    )}
                    {assignedRemainingWeight !== null && data.fillLevel !== null && (
                      <span className="text-[9px] text-bambu-gray font-normal">• {assignedRemainingWeight}g</span>
                    )}
                  </span>
                </div>
                {/* Fill bar */}
                <div className="h-1.5 bg-black/40 rounded-full overflow-hidden">
                  {data.fillLevel !== null ? (
                    <div
                      className="h-full rounded-full transition-all duration-300"
                      style={{
                        width: `${data.fillLevel}%`,
                        backgroundColor: getFillColor(data.fillLevel),
                      }}
                    />
                  ) : (
                    <div className="h-full w-full bg-bambu-gray/30 rounded-full" />
                  )}
                </div>
              </div>

              {/* Spoolman section - only show if enabled */}
              {spoolman?.enabled && (
                <div className="pt-2 mt-2 border-t border-bambu-dark-tertiary space-y-2">
                  {/* Tray UUID with copy button */}
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">
                      {t('spoolman.spoolId')}
                    </span>
                    {data.trayUuid ? (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          handleCopyUuid();
                        }}
                        className="flex items-center gap-1 text-xs text-bambu-gray hover:text-white transition-colors"
                        title="Copy spool UUID"
                      >
                        <span className="font-mono text-[10px] truncate max-w-[80px]">
                          {data.trayUuid.slice(0, 8)}...
                        </span>
                        {copied ? (
                          <Check className="w-3 h-3 text-bambu-green" />
                        ) : (
                          <Copy className="w-3 h-3" />
                        )}
                      </button>
                    ) : (
                      <span className="text-[10px] text-bambu-gray">-</span>
                    )}
                  </div>

                  {/* Open in inventory button (when already linked to a Spoolman spool) */}
                  {spoolman.linkedSpoolId && (
                    <>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          dismiss();
                          navigate(`/inventory?spool=${spoolman.linkedSpoolId}`);
                        }}
                        className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-green/20 hover:bg-bambu-green/40 text-bambu-green"
                        title={t('inventory.openInInventory')}
                      >
                        <Package className="w-3.5 h-3.5" />
                        {t('inventory.openInInventory')}
                      </button>

                    </>
                  )}

                  {/* Link/Unlink action buttons intentionally NOT rendered
                      here. The inventory section below already provides
                      Assign/Unassign for slot-binding (the primary user
                      flow in Spoolman mode). Showing the spoolman tag-link
                      buttons in addition surfaced two red Unlink-icon
                      buttons for what users perceive as the same action,
                      regardless of whether the labels said "Unlink Spool"
                      vs "Unassign Spool". Tag-linking remains available
                      via dedicated UI (LinkSpoolModal can be opened from
                      Spoolman settings / inventory page). */}
                </div>
              )}

              {/* Inventory section — shown for every vendor including
                  Bambu Lab (#1133). The earlier "non-Bambu only" gate
                  prevented users from manually assigning a Bambu spool
                  in inventory to an AMS slot when they didn't want to
                  re-scan via RFID. */}
              {inventory && (
                <div className="pt-2 mt-2 border-t border-bambu-dark-tertiary space-y-2">
                  {inventory.assignedSpool ? (
                    <>
                      <div className="flex items-center gap-1.5">
                        <Package className="w-3 h-3 text-bambu-green" />
                        <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">
                          {t('inventory.assigned')}
                        </span>
                      </div>
                      <div className="flex items-baseline gap-1.5 min-w-0">
                        <p className="text-xs text-white truncate" title={inventory.assignedSpool.displayName}>
                          {inventory.assignedSpool.displayName}
                        </p>
                        {/* Spool ID next to the display name so operators
                            can reference it without opening the edit modal
                            (upstream Bambuddy #1385). */}
                        <span className="text-[10px] font-mono text-bambu-gray shrink-0">#{inventory.assignedSpool.id}</span>
                      </div>
                      {(!spoolman?.linkedSpoolId || inventory.assignedSpool!.id !== spoolman.linkedSpoolId) && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            dismiss();
                            navigate(`/inventory?spool=${inventory.assignedSpool!.id}`);
                          }}
                          className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-green/20 hover:bg-bambu-green/40 text-bambu-green"
                          title={t('inventory.openInInventory')}
                        >
                          <Package className="w-3.5 h-3.5" />
                          {t('inventory.openInInventory')}
                        </button>
                      )}
                      {/* Replace, not Unassign-then-Assign. Both assign
                          endpoints have always replaced the spool on an
                          occupied slot, and the accounting the operator needs
                          (the usage journal's assignment change, the mid-print
                          replacement prompt) lives on the assign side — the
                          unassign endpoints do none of it. So the one-step
                          path is also the accounted one. It reuses
                          `onAssignSpool`, which the page passes for assigned
                          slots too: Replace appears exactly where Assign
                          would, including behind the internal branch's
                          non-Bambu gate. */}
                      {inventory.onAssignSpool && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            dismiss();
                            inventory.onAssignSpool?.();
                          }}
                          className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-blue/20 hover:bg-bambu-blue/40 text-bambu-blue"
                        >
                          <Repeat className="w-3.5 h-3.5" />
                          {t('inventory.replaceSpool')}
                        </button>
                      )}
                      {inventory.onUnassignSpool && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            dismiss();
                            inventory.onUnassignSpool?.();
                          }}
                          className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-red-100 dark:bg-red-500/20 hover:bg-red-200 dark:hover:bg-red-500/40 text-red-700 dark:text-red-400"
                        >
                          <Unlink className="w-3.5 h-3.5" />
                          {t('inventory.unassignSpool')}
                        </button>
                      )}
                    </>
                  ) : inventory.unavailableReason ? (
                    <>
                      <button
                        disabled
                        className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded bg-bambu-blue/20 text-bambu-blue opacity-50 cursor-not-allowed"
                      >
                        <Package className="w-3.5 h-3.5" />
                        {t('inventory.assignSpool')}
                      </button>
                      <p className="text-[10px] text-bambu-gray">{inventory.unavailableReason}</p>
                    </>
                  ) : inventory.onAssignSpool ? (
                    <button
                      onClick={inventory.isAssigned ? undefined : (e) => {
                        e.stopPropagation();
                        dismiss();
                        inventory.onAssignSpool?.();
                      }}
                      disabled={!!inventory.isAssigned}
                      className={`w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-blue/20 text-bambu-blue ${
                        inventory.isAssigned ? 'opacity-50 cursor-not-allowed' : 'hover:bg-bambu-blue/40'
                      }`}
                    >
                      <Package className="w-3.5 h-3.5" />
                      {t('inventory.assignSpool')}
                    </button>
                  ) : null}
                </div>
              )}

              {/* Configure slot section - always show if enabled */}
              {configureSlot?.enabled && (
                <div className={`${spoolman?.enabled && data.trayUuid ? '' : 'pt-2 mt-2 border-t border-bambu-dark-tertiary'}`}>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      dismiss();
                      configureSlot.onConfigure?.();
                    }}
                    className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-blue/20 hover:bg-bambu-blue/40 text-bambu-blue"
                    title={t('ams.configureSlot')}
                  >
                    <Settings2 className="w-3.5 h-3.5" />
                    {t('ams.configure')}
                  </button>
                </div>
              )}
            </div>
          </div>

          {/* Arrow pointer — still aimed at the trigger after the card was
              clamped sideways; the hook hands us the offset directly now
              instead of us undoing a marginLeft. */}
          <div
            className={`
              absolute -translate-x-1/2 w-0 h-0
              border-l-[6px] border-l-transparent
              border-r-[6px] border-r-transparent
              ${placement?.side === 'bottom'
                ? 'bottom-full border-b-[6px] border-b-bambu-dark-tertiary'
                : 'top-full border-t-[6px] border-t-bambu-dark-tertiary'}
            `}
            style={{ left: `${placement?.arrowLeft ?? 0}px` }}
          />
        </div>,
        document.body,
      )}

      {/* Unlink Confirmation Dialog */}
      {showUnlinkConfirm && (
        <Modal
          onClose={() => setShowUnlinkConfirm(false)}
          hideClose
          ariaLabel={t('spoolman.unlinkConfirmTitle')}
          size="sm"
        >
          <div className="p-4 space-y-4">
            <div className="space-y-2">
              <h3 className="text-base font-semibold text-white">
                {t('spoolman.unlinkConfirmTitle')}
              </h3>
              <p className="text-sm text-bambu-gray">
                {t('spoolman.unlinkConfirmMessage')}
              </p>
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => setShowUnlinkConfirm(false)}
                className="flex-1 px-3 py-2 text-sm font-medium rounded transition-colors bg-bambu-dark hover:bg-bambu-dark-tertiary text-white"
              >
                {t('common.cancel')}
              </button>
              <button
                onClick={() => {
                  spoolman?.onUnlinkSpool?.();
                  setShowUnlinkConfirm(false);
                }}
                className="flex-1 px-3 py-2 text-sm font-medium rounded transition-colors bg-red-100 dark:bg-red-500/20 hover:bg-red-200 dark:hover:bg-red-500/40 text-red-700 dark:text-red-400"
              >
                {t('inventory.unassignSpool')}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

interface EmptySlotHoverCardProps {
  children: ReactNode;
  className?: string;
  configureSlot?: ConfigureSlotConfig;
  onAssignSpool?: () => void;
  /** 'reset' ⇒ a spool is loaded but its type isn't configured; the header
   *  reads "slot not configured" instead of "empty slot" (#1694). */
  emptyKind?: 'physical' | 'reset' | null;
  /** The spool BamDude holds for a slot the printer reports blank — one put in
   *  after a runout, or assigned before the reel went in. The card names it;
   *  `onAssignSpool` then replaces it instead of assigning. */
  assigned?: { label: string; colorHex?: string | null; onUnassign?: () => void } | null;
}

export function EmptySlotHoverCard({ children, className = '', configureSlot, onAssignSpool, emptyKind, assigned }: EmptySlotHoverCardProps) {
  const { t } = useTranslation();
  const { isVisible, triggerRef, cardRef, cardId, triggerProps, hoverProps, dismiss } = useSlotCard();
  const placement = useCardPlacement(isVisible, triggerRef, cardRef);

  return (
    <div
      {...triggerProps}
      className={`relative ${className}`}
      data-testid="empty-slot"
    >
      {children}

      {isVisible && createPortal(
        <div
          ref={cardRef}
          {...hoverProps}
          id={cardId}
          role="dialog"
          aria-label={t('ams.slotDetails')}
          tabIndex={-1}
          data-ams-slot-card=""
          data-testid="empty-slot-hover-card"
          className="absolute z-[60] animate-in fade-in-0 zoom-in-95"
          style={{
            // ⚠️ Never transition the position. A bare `duration-*` class sets
            // `transition-duration` while `transition-property` stays at its
            // initial `all`, so `top`/`left` animated from the viewport origin
            // and the card visibly flew in from the top-left corner. Harmless
            // while the position came from static classes; not harmless now
            // that it is measured and assigned. The enter animation is a
            // keyframe (`animate-in`), which is unaffected.
            transition: 'none',
            top: placement?.top ?? 0,
            left: placement?.left ?? 0,
            visibility: placement ? 'visible' : 'hidden',
            maxWidth: 'calc(100vw - 16px)',
          }}
        >
          <div className="
            bg-bambu-dark-secondary border border-bambu-dark-tertiary
            rounded-md shadow-lg overflow-y-auto
          " style={{ maxHeight: placement?.maxHeight }}>
            {assigned ? (
              <div className="px-3 py-1.5 text-xs space-y-1">
                <div className="flex items-center gap-1.5 text-white">
                  <span
                    className="w-2.5 h-2.5 rounded-full border border-dashed border-amber-500 flex-shrink-0"
                    style={{ backgroundColor: assigned.colorHex ? `#${assigned.colorHex.slice(0, 6)}` : 'transparent' }}
                  />
                  <span className="min-w-0 break-words">{assigned.label}</span>
                </div>
                <div className="text-amber-700 dark:text-amber-400 max-w-[14rem] whitespace-normal">{t('ams.assignedNotConfigured')}</div>
              </div>
            ) : (
              <div className="px-3 py-1.5 text-xs">
                {emptyKind === 'reset' ? (
                  <span className="text-amber-700 dark:text-amber-400">{t('ams.slotLoadedUnconfigured')}</span>
                ) : (
                  <span className="text-bambu-gray">{t('ams.emptySlot')}</span>
                )}
              </div>
            )}
            {/* Configure slot button */}
            {(configureSlot?.enabled || onAssignSpool || assigned?.onUnassign) && (
              <div className="px-2 pb-2 space-y-1">
                {/* ⚠️ Assign BEFORE Configure, matching the filled-slot card
                    above. The two cards are separate render paths and had
                    drifted into opposite orders, so the menu reshuffled itself
                    depending on whether the slot happened to hold filament. */}
                {onAssignSpool && (
                  <button
                    onClick={(e) => { e.stopPropagation(); dismiss(); onAssignSpool(); }}
                    className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-blue/20 hover:bg-bambu-blue/40 text-bambu-blue"
                  >
                    {assigned ? <Repeat className="w-3.5 h-3.5" /> : <Package className="w-3.5 h-3.5" />}
                    {assigned ? t('inventory.replaceSpool') : t('inventory.assignSpool')}
                  </button>
                )}
                {assigned?.onUnassign && (
                  <button
                    onClick={(e) => { e.stopPropagation(); dismiss(); assigned.onUnassign?.(); }}
                    className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-red-100 dark:bg-red-500/20 hover:bg-red-200 dark:hover:bg-red-500/40 text-red-700 dark:text-red-400"
                  >
                    <Unlink className="w-3.5 h-3.5" />
                    {t('inventory.unassignSpool')}
                  </button>
                )}
                {configureSlot?.enabled && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      dismiss();
                      configureSlot.onConfigure?.();
                    }}
                    className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-blue/20 hover:bg-bambu-blue/40 text-bambu-blue"
                    title={t('ams.configureSlot')}
                  >
                    <Settings2 className="w-3.5 h-3.5" />
                    {t('ams.configure')}
                  </button>
                )}
              </div>
            )}
          </div>
          <div
            className={`
              absolute -translate-x-1/2 w-0 h-0
              border-l-[5px] border-l-transparent
              border-r-[5px] border-r-transparent
              ${placement?.side === 'bottom'
                ? 'bottom-full border-b-[5px] border-b-bambu-dark-tertiary'
                : 'top-full border-t-[5px] border-t-bambu-dark-tertiary'}
            `}
            style={{ left: `${placement?.arrowLeft ?? 0}px` }}
          />
        </div>,
        document.body,
      )}
    </div>
  );
}
