import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';
import { ChevronDown, Trash2 } from 'lucide-react';

interface TrashSplitButtonProps {
  trashHref: string;
  trashLabel: string;
  trashTooltip?: string;
  count?: number;
  /** When omitted (no purge permission), the caret half is suppressed and
   *  the component collapses back to a single-button trash link. */
  onPurgeClick?: () => void;
  purgeLabel?: string;
  purgeTooltip?: string;
}

/**
 * Header trash control for FileManager / Archives. The main half links to
 * the trash list (with the unread-count chip); the caret half opens a
 * one-item portal menu that fires the bulk "purge by age" modal.
 *
 * Both pages used to render two adjacent buttons; folding the rarely-used
 * "purge old" into a side menu keeps the toolbar tighter without hiding
 * the action.
 */
export function TrashSplitButton({
  trashHref,
  trashLabel,
  trashTooltip,
  count,
  onPurgeClick,
  purgeLabel,
  purgeTooltip,
}: TrashSplitButtonProps) {
  const [open, setOpen] = useState(false);
  const caretRef = useRef<HTMLButtonElement | null>(null);
  const [coords, setCoords] = useState<{ top: number; right: number } | null>(null);

  useEffect(() => {
    if (!open) return;
    const update = () => {
      const btn = caretRef.current;
      if (!btn) return;
      const rect = btn.getBoundingClientRect();
      setCoords({
        top: rect.bottom + 4,
        right: Math.max(8, window.innerWidth - rect.right),
      });
    };
    update();
    window.addEventListener('resize', update);
    window.addEventListener('scroll', update, true);
    return () => {
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', update, true);
    };
  }, [open]);

  const hasCaret = !!onPurgeClick;
  const baseCls =
    'inline-flex items-center px-3 py-1.5 text-sm border border-bambu-dark-tertiary bg-bambu-dark-secondary text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors';

  return (
    <div className="inline-flex">
      <Link
        to={trashHref}
        className={`${baseCls} ${hasCaret ? 'rounded-l-lg border-r-0' : 'rounded-lg'}`}
        title={trashTooltip}
      >
        <Trash2 className="w-4 h-4 mr-2" />
        {trashLabel}
        {typeof count === 'number' && count > 0 && (
          <span className="ml-1.5 px-1.5 py-0.5 text-xs rounded-full bg-bambu-green/20 text-bambu-green">
            {count}
          </span>
        )}
      </Link>
      {hasCaret && (
        <>
          <button
            ref={caretRef}
            type="button"
            onClick={() => setOpen((v) => !v)}
            className={`${baseCls} rounded-r-lg px-1.5`}
            aria-label={purgeLabel ?? purgeTooltip ?? 'More'}
            aria-haspopup="menu"
            aria-expanded={open}
            title={purgeTooltip}
          >
            <ChevronDown className="w-4 h-4" />
          </button>
          {open && createPortal(
            <>
              <div className="fixed inset-0 z-[55]" onClick={() => setOpen(false)} />
              <div
                style={{
                  position: 'fixed',
                  top: coords?.top ?? 0,
                  right: coords?.right ?? 0,
                  visibility: coords ? 'visible' : 'hidden',
                }}
                className="z-[60] bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl py-1 w-[240px] whitespace-nowrap"
                role="menu"
              >
                <button
                  type="button"
                  className="w-full px-3 py-1.5 text-left text-sm flex items-center gap-2 text-white hover:bg-bambu-dark"
                  onClick={() => {
                    setOpen(false);
                    onPurgeClick?.();
                  }}
                  role="menuitem"
                  title={purgeTooltip}
                >
                  <Trash2 className="w-3.5 h-3.5" />
                  {purgeLabel}
                </button>
              </div>
            </>,
            document.body,
          )}
        </>
      )}
    </div>
  );
}
