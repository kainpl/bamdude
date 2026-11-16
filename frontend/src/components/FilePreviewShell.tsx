/**
 * The shell every file preview is shown in: the lightbox ground, a bar with the
 * file's name and the controls, and a stage the renderer fills.
 *
 * One shell, a renderer per file type — the image viewer today; a renderer for
 * another type brings its own `controls` and its own stage and gets the rest
 * (the dialog, Esc through the modal stack, real fullscreen, the close button)
 * from here.
 *
 * Fullscreen is the shell's own box — the bar and the stage — so the way out
 * and the close button stay on screen in it. ⚠️ A renderer must open no dialog
 * of its own: one portalled into `body` is not part of what fullscreen shows.
 *
 * It is `Modal variant="lightbox"`: the ground closes on a click. The shell's
 * own box covers that ground, so "a click on the ground" is the RENDERER's
 * call — it knows which part of its stage is empty and whether the click ended
 * a drag — and it gets `onClose` for exactly that.
 */
import { useRef, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { Modal } from './Modal';
import { FullscreenButton } from './FullscreenButton';
import { useElementFullscreen } from '../hooks/useElementFullscreen';

export interface FilePreviewShellProps {
  /** The file's name: the visible heading and the dialog's accessible name. */
  title: string;
  onClose: () => void;
  /** The renderer's own controls (zoom, page…), left of the shell's. */
  controls?: ReactNode;
  /** The renderer. It fills the stage: `absolute inset-0` or `h-full w-full`. */
  children: ReactNode;
}

/** A control in the preview bar — one look for the shell's and the renderers'. */
export const PREVIEW_CONTROL_CLASS =
  'inline-flex h-9 min-w-9 items-center justify-center rounded px-2 text-sm text-bambu-gray transition-colors hover:bg-white/10 hover:text-white disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent';

export function FilePreviewShell({ title, onClose, controls, children }: FilePreviewShellProps) {
  const { t } = useTranslation();
  const boxRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const fullscreen = useElementFullscreen(boxRef, toggleRef);
  return (
    <Modal variant="lightbox" onClose={onClose} ariaLabel={title}>
      {/* In fullscreen the lightbox's dark ground is no longer behind the box. */}
      <div ref={boxRef} className={`relative flex h-full w-full flex-col ${fullscreen.active ? 'bg-black' : ''}`}>
        <div className="flex shrink-0 items-center justify-between gap-3 bg-black/50 px-4 py-2">
          <h2 className="min-w-0 truncate text-base font-semibold text-white" title={title}>
            {title}
          </h2>
          <div className="flex shrink-0 items-center gap-1">
            {controls}
            <FullscreenButton ref={toggleRef} fullscreen={fullscreen} className={PREVIEW_CONTROL_CLASS} />
            <button type="button" aria-label={t('common.close')} onClick={onClose} className={PREVIEW_CONTROL_CLASS}>
              <X className="h-5 w-5" />
            </button>
          </div>
        </div>
        <div className="relative min-h-0 flex-1 overflow-hidden">{children}</div>
      </div>
    </Modal>
  );
}
