import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Boxes, Spline } from 'lucide-react';

import { GcodeViewer } from './GcodeViewer';
import { GcodeToolpathViewer } from './GcodeToolpathViewer';

/**
 * Picks which G-code renderer draws a file, and lets the operator switch.
 *
 * Two live side by side on purpose. ``GcodeViewer`` is the one that has been
 * shipping — `gcode-preview` drawing screen-space lines. ``GcodeToolpathViewer``
 * is OrcaSlicer's own `libvgcode`, which builds a diamond-section prism per
 * segment so the print occludes itself, and can colour by feature, layer height
 * or line width rather than only by filament.
 *
 * ⚠️ **The classic one is the default, deliberately.** The new renderer is the
 * better picture but not yet a superset: it has no play/pause animation, no
 * Export PNG, no theme (its canvas is always dark) and no download progress for
 * a large file. Defaulting to it would trade five working things for a nicer
 * image. When those land, the default flips — and the switch stays, because a
 * renderer is exactly the kind of thing that meets a file it cannot draw.
 *
 * The choice is per browser (``localStorage``) rather than a server setting:
 * what this is for is opening the SAME file both ways and comparing, not
 * setting a policy for an installation.
 */

const RENDERER_KEY = 'bd-gcode-renderer';

type Renderer = 'classic' | 'toolpath';

function storedRenderer(): Renderer {
  try {
    return localStorage.getItem(RENDERER_KEY) === 'toolpath' ? 'toolpath' : 'classic';
  } catch {
    // Private mode / storage disabled — the default is a working viewer.
    return 'classic';
  }
}

interface GcodePreviewProps {
  gcodeUrl: string;
  buildVolume?: { x: number; y: number; z: number };
  filamentColors?: string[];
  theme?: 'light' | 'dark';
  exportFilename?: string;
  className?: string;
}

export function GcodePreview({
  gcodeUrl,
  buildVolume,
  filamentColors,
  theme,
  exportFilename,
  className = '',
}: GcodePreviewProps) {
  const { t } = useTranslation();
  const [renderer, setRenderer] = useState<Renderer>(storedRenderer);

  const toggle = useCallback(() => {
    setRenderer((current) => {
      const next: Renderer = current === 'toolpath' ? 'classic' : 'toolpath';
      try {
        localStorage.setItem(RENDERER_KEY, next);
      } catch {
        /* storage unavailable — the choice just doesn't outlive the session */
      }
      return next;
    });
  }, []);

  const isToolpath = renderer === 'toolpath';

  return (
    <div className={`relative ${className}`}>
      {isToolpath ? (
        <GcodeToolpathViewer
          gcodeUrl={gcodeUrl}
          buildVolume={buildVolume}
          filamentColors={filamentColors}
          className="w-full h-full"
        />
      ) : (
        <GcodeViewer
          gcodeUrl={gcodeUrl}
          buildVolume={buildVolume}
          filamentColors={filamentColors}
          theme={theme}
          exportFilename={exportFilename}
          className="w-full h-full"
        />
      )}

      {/* Over the canvas, which both viewers keep clear in this corner — their
          own controls sit below it. */}
      <button
        type="button"
        onClick={toggle}
        title={
          isToolpath
            ? t('gcodeViewer.switchToClassic', 'Switch to the classic renderer')
            : t('gcodeViewer.switchToToolpath', 'Switch to the slicer renderer')
        }
        className="absolute top-2 right-2 z-10 flex items-center gap-1.5 rounded-md bg-bambu-dark/80 px-2 py-1 text-xs text-bambu-gray backdrop-blur transition-colors hover:text-white"
      >
        {isToolpath ? <Spline className="w-3.5 h-3.5" /> : <Boxes className="w-3.5 h-3.5" />}
        {isToolpath
          ? t('gcodeViewer.rendererClassic', 'Classic')
          : t('gcodeViewer.rendererToolpath', 'Solid')}
      </button>
    </div>
  );
}
