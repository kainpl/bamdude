/**
 * The modal shell. Every modal in the app renders through it (a source guard,
 * `modalShellOwnership.test.ts`, fails on any full-screen overlay outside it).
 *
 * What it owns, so that no modal has to:
 * - the overlay and the panel — portalled into `document.body`; z-index is
 *   `50 + position in the modal stack`, never hand-written, so paint order
 *   follows the stack (see `modalStack.ts` for why neither DOM order nor
 *   nesting depth is the right order);
 * - closing: the header X and Esc (through `modalStack`, topmost modal only).
 *   ⚠️ The backdrop has NO click handler — a tap outside a form must not throw
 *   the form away. `variant="lightbox"` is the one exception.
 * - `role="dialog"`, `aria-modal`, the accessible name, `tabIndex={-1}` and
 *   focus in/out (`useDialogFocus`);
 * - the focus trap: while it is open the stack marks `#root` inert, and marks
 *   this modal's overlay inert whenever another modal sits above it
 *   (`modalStack.ts` owns every `inert` attribute of the modal layers). Tab
 *   therefore cannot reach the page behind the topmost modal — no key handler
 *   of our own is involved; the toast viewport and popovers portalled into
 *   body stay live on purpose.
 *
 * The body has no padding of its own: a migrated modal keeps its inner markup
 * exactly as it was. `size` is a closed table of literal classes — Tailwind's
 * scanner cannot see a template string; `8xl` needs `--container-8xl` in
 * `index.css`.
 */
import { useId, useRef, type CSSProperties, type MouseEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { useDialogFocus } from '../hooks/useDialogFocus';
import { ModalAncestryContext, useModalStackEntry } from './modalStack';

export type ModalSize = 'xs' | 'sm' | 'md' | 'lg' | 'xl' | '2xl' | '3xl' | '4xl' | '5xl' | '6xl' | '7xl' | '8xl' | 'full';

export const MODAL_SIZE_CLASS: Record<ModalSize, string> = {
  xs: 'max-w-xs',
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-xl',
  '2xl': 'max-w-2xl',
  '3xl': 'max-w-3xl',
  '4xl': 'max-w-4xl',
  '5xl': 'max-w-5xl',
  '6xl': 'max-w-6xl',
  '7xl': 'max-w-7xl',
  '8xl': 'max-w-8xl',
  full: 'h-full max-w-none rounded-none border-0',
};

export interface ModalProps {
  /** The header X and Esc both land here. */
  onClose: () => void;
  /** Standard header: `icon` + this title + the X. */
  title?: ReactNode;
  icon?: ReactNode;
  /** Custom header content; the shell still appends the X. Pair with `labelledBy`. */
  header?: ReactNode;
  /** id of the visible heading when `header` is used. */
  labelledBy?: string;
  describedBy?: string;
  /** Panel width; default `md`. `full` fills the viewport. */
  size?: ModalSize;
  /** `lightbox`: dark ground, no chrome, a click on the ground closes. */
  variant?: 'dialog' | 'lightbox';
  /** In-flight: the X is disabled and Esc is ignored. */
  closeDisabled?: boolean;
  /** No X — only when the body carries its own Cancel/Close button. */
  hideClose?: boolean;
  footer?: ReactNode;
  panelClassName?: string;
  /** Inline panel style for geometry that is computed, not a class — a width derived from an image size. Beats every class. */
  panelStyle?: CSSProperties;
  bodyClassName?: string;
  /** Accessible name when there is no title (lightboxes, `hideClose` cards). */
  ariaLabel?: string;
  children: ReactNode;
}

export function Modal({
  onClose,
  title,
  icon,
  header,
  labelledBy,
  describedBy,
  size = 'md',
  variant = 'dialog',
  closeDisabled = false,
  hideClose = false,
  footer,
  panelClassName = '',
  panelStyle,
  bodyClassName = '',
  ariaLabel,
  children,
}: ModalProps) {
  const { t } = useTranslation();
  const titleId = useId();
  // The outermost portalled element: the stack marks it inert while another
  // modal is above this one (modalStack.ts owns the attribute).
  const overlayRef = useRef<HTMLDivElement>(null);
  // ⚠️ Declared BEFORE useDialogFocus on purpose. React runs a component's
  // effect cleanups in declaration order: on unmount this one unregisters
  // first, which makes #root (or the modal below) live again, and only then
  // does useDialogFocus give focus back — focus() into an inert subtree is a
  // silent no-op. Modal.test.tsx pins the order.
  const { position, childAncestry } = useModalStackEntry({ onClose, closeDisabled }, overlayRef);
  const focusRef = useDialogFocus<HTMLDivElement>(true);
  // Computed, never hand-written: paint order follows the stack.
  const zIndex = 50 + position;
  const body = <ModalAncestryContext.Provider value={childAncestry}>{children}</ModalAncestryContext.Provider>;

  if (variant === 'lightbox') {
    const onGroundClick = (e: MouseEvent<HTMLDivElement>) => {
      e.stopPropagation();
      if (e.target === e.currentTarget && !closeDisabled) onClose();
    };
    // The ground is both the overlay and the dialog, so it carries both refs.
    const groundRef = (el: HTMLDivElement | null) => {
      overlayRef.current = el;
      focusRef.current = el;
    };
    return createPortal(
      <div
        ref={groundRef}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        tabIndex={-1}
        style={{ zIndex }}
        className="fixed inset-0 flex items-center justify-center bg-black/90 backdrop-blur-sm outline-none"
        onClick={onGroundClick}
      >
        {body}
      </div>,
      document.body,
    );
  }

  const showHeader = title !== undefined || header !== undefined || !hideClose;
  const labelled = labelledBy ?? (title !== undefined ? titleId : undefined);
  // React events bubble through the React tree even out of a portal, and
  // modals are opened from clickable cards — nothing inside may reach them.
  const stopClick = (e: MouseEvent<HTMLDivElement>) => e.stopPropagation();

  return createPortal(
    <div
      ref={overlayRef}
      style={{ zIndex }}
      className={`fixed inset-0 flex items-center justify-center bg-black/50 backdrop-blur-sm ${size === 'full' ? '' : 'p-4'}`}
      onClick={stopClick}
    >
      <div
        ref={focusRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelled}
        aria-describedby={describedBy}
        aria-label={labelled ? undefined : ariaLabel}
        tabIndex={-1}
        style={panelStyle}
        className={`relative flex w-full flex-col rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary shadow-2xl outline-none ${size === 'full' ? '' : 'max-h-[90vh]'} ${MODAL_SIZE_CLASS[size]} ${panelClassName}`}
      >
        {showHeader && (
          <div className="flex shrink-0 items-center justify-between gap-3 border-b border-bambu-dark-tertiary px-4 py-4">
            <div className="flex min-w-0 flex-1 items-center gap-2">
              {header !== undefined ? (
                header
              ) : (
                <>
                  {icon}
                  {title !== undefined && (
                    <h2 id={titleId} className="text-lg font-semibold text-white">
                      {title}
                    </h2>
                  )}
                </>
              )}
            </div>
            {!hideClose && (
              <button
                type="button"
                aria-label={t('common.close')}
                disabled={closeDisabled}
                onClick={onClose}
                className="shrink-0 rounded p-1 text-bambu-gray transition-colors hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
              >
                <X className="h-5 w-5" />
              </button>
            )}
          </div>
        )}
        <div className={`min-h-0 flex-1 overflow-y-auto ${bodyClassName}`}>{body}</div>
        {footer !== undefined && (
          <div className="flex shrink-0 items-center justify-end gap-2 border-t border-bambu-dark-tertiary px-4 py-3">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
