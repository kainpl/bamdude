import { AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useSlicerHealth, type SlicerKind } from '../hooks/useSlicerHealth';

/**
 * Says that the picked slicer's container is older than this BamDude needs.
 *
 * ⚠️ The slicer containers are built from our sidecar fork whenever the
 * operator rebuilds them, on their own schedule — so a BamDude update does not
 * bring a newer sidecar with it, and nothing else on screen would say why a
 * preset is missing from the list or why a slice is not refused where it
 * should be. The backend compares the sidecar's code version with the one it
 * needs and answers `sidecar_outdated`; this is the one place that turns it
 * into the action that fixes it.
 *
 * ⚠️ It informs and never blocks: an older container still slices, as far as
 * it can. Nothing is said about an offline container — its card already says
 * so, and "rebuild" is advice for one that answered.
 *
 * Both health queries are read (hooks cannot be conditional); they are the
 * same cache entries the slicer cards use, so this costs no request.
 */
export function SlicerOutdatedNote({ kind }: { kind: SlicerKind | null }) {
  const { t } = useTranslation();
  const orca = useSlicerHealth('orcaslicer');
  const bambu = useSlicerHealth('bambu_studio');
  const health = kind === 'orcaslicer' ? orca.data : kind === 'bambu_studio' ? bambu.data : undefined;
  if (!health?.healthy || !health.sidecar_outdated) return null;
  return (
    <p
      role="status"
      className="flex items-start gap-2 text-xs rounded-md border p-2 mt-2 border-amber-300 dark:border-amber-700/40 bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200"
    >
      <AlertTriangle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" aria-hidden="true" />
      <span className="min-w-0 break-words">{t('slicerHealth.sidecarOutdated')}</span>
    </p>
  );
}
