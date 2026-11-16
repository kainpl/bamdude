/**
 * The toggle for real fullscreen (`useElementFullscreen`).
 *
 * Renders NOTHING where the browser has no element fullscreen — an iPhone gets
 * no button rather than one that does nothing. ⚠️ Place it inside the element
 * that goes fullscreen: in fullscreen it is the only way out besides Escape.
 * Its icon is deliberately not the "fill the window" one.
 */
import { forwardRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Expand, Shrink } from 'lucide-react';
import type { ElementFullscreen } from '../hooks/useElementFullscreen';

interface Props {
  fullscreen: ElementFullscreen;
  className?: string;
}

export const FullscreenButton = forwardRef<HTMLButtonElement, Props>(function FullscreenButton(
  { fullscreen, className },
  ref,
) {
  const { t } = useTranslation();
  if (!fullscreen.supported) return null;
  const label = fullscreen.active ? t('filePreview.exitFullscreen') : t('filePreview.fullscreen');
  return (
    <button
      ref={ref}
      type="button"
      onClick={fullscreen.toggle}
      aria-pressed={fullscreen.active}
      aria-label={label}
      title={label}
      className={className}
    >
      {fullscreen.active ? <Shrink className="h-5 w-5" /> : <Expand className="h-5 w-5" />}
    </button>
  );
});
