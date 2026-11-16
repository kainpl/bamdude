/**
 * The label as it will come out, before anything is printed.
 *
 * ⚠️ **Drawn by the server, never here.** The picture comes from the renderer
 * that does the printing — the PDF backend rasterised for a driver, the one-bit
 * raster for a desk printer — because a label drawn a second way in the browser
 * is a second layout, and it would disagree with the first.
 *
 * ⚠️ **Shown, not fitted.** The picture is displayed smaller or larger to suit
 * the dialog; the label itself is never scaled.
 *
 * It is fetched as a blob with the session's headers, not by an `<img src>`
 * URL, so no token rides in a query string. The object URL is revoked when it
 * is replaced and when the dialog goes.
 */

import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import type { LabelPreview } from '../../api/client';

/** Long enough that flicking through the designs asks for one picture. */
const DEBOUNCE_MS = 300;

interface LabelPicturePreviewProps {
  /** Names what is shown: when it changes the picture is drawn again, and the
   *  one still on its way is cancelled. */
  requestKey: string;
  load: (signal: AbortSignal) => Promise<LabelPreview>;
  /** Whose label this is, for the picture's description. */
  name: string;
  /** One line under the title — which spool, which printer. */
  caption?: string;
}

type Shown =
  | { state: 'drawing' }
  | { state: 'drawn'; url: string; warnings: string[] }
  | { state: 'refused'; message: string };

export function LabelPicturePreview({ requestKey, load, name, caption }: LabelPicturePreviewProps) {
  const { t } = useTranslation();
  const [shown, setShown] = useState<Shown>({ state: 'drawing' });
  const urlRef = useRef<string | null>(null);
  // The newest loader, without making every render a reason to fetch again.
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    const abort = new AbortController();
    setShown({ state: 'drawing' });
    const timer = window.setTimeout(async () => {
      try {
        const { blob, warnings } = await loadRef.current(abort.signal);
        if (abort.signal.aborted) return;
        const url = window.URL.createObjectURL(blob);
        if (urlRef.current) window.URL.revokeObjectURL(urlRef.current);
        urlRef.current = url;
        setShown({ state: 'drawn', url, warnings });
      } catch (error) {
        if (abort.signal.aborted) return;
        setShown({ state: 'refused', message: error instanceof Error ? error.message : String(error) });
      }
    }, DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
      abort.abort();
    };
  }, [requestKey]);

  useEffect(
    () => () => {
      if (urlRef.current) window.URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    },
    [],
  );

  return (
    <div className="p-2.5 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark space-y-2">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-xs font-medium text-white">{t('inventory.labels.preview.title')}</span>
        {caption && <span className="text-xs text-bambu-gray min-w-0 truncate">{caption}</span>}
      </div>

      {shown.state === 'drawing' && (
        <p className="flex items-center gap-2 text-xs text-bambu-gray py-3" role="status">
          <Loader2 className="w-4 h-4 animate-spin shrink-0" />
          {t('inventory.labels.preview.drawing')}
        </p>
      )}

      {shown.state === 'refused' && (
        <p className="text-xs text-amber-600 dark:text-amber-400 py-1" role="status">
          {t('inventory.labels.preview.refused', { msg: shown.message })}
        </p>
      )}

      {shown.state === 'drawn' && (
        <>
          {/* Paper is white whatever the theme — this is a picture of a label. */}
          <img
            src={shown.url}
            alt={t('inventory.labels.preview.alt', { name })}
            className="block mx-auto max-w-full max-h-44 w-auto h-auto object-contain bg-white rounded border border-black/20"
          />
          {shown.warnings.length > 0 && (
            <ul className="text-xs text-amber-600 dark:text-amber-400 space-y-0.5">
              {shown.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
