import { FileWarning, Link, Loader2, ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { QueueSourceStorage } from '../api/client';
import { isSelfContained, markedStorage } from '../utils/queueSource';

interface QueueSourceIndicatorProps {
  /** The row's own `source_storage`. Absent / `exempt` renders nothing. */
  state: QueueSourceStorage | null | undefined;
  /**
   * This row is failed / skipped / cancelled and keeps its file until it is
   * removed — the one sentence §10 asks for, said where the operator is already
   * looking at the row that holds it. Deliberately not a "clean up everything"
   * button: the retry is the reason the bytes are still there.
   *
   * ⚠️ **Ignored unless the row actually HAS a stored file.** Appending it to a
   * `legacy` row read "it reads the original file … the stored file stays with
   * this job", and to a `broken` one "the stored copy is missing … the stored
   * file stays with this job" — two sentences contradicting each other, and on
   * the `legacy` row the ORIGINAL is the thing still load-bearing.
   */
  held?: boolean;
  /** Extra classes for the icon; the default fits a compact queue row. */
  className?: string;
  testId?: string;
}

const ICONS = {
  ready: ShieldCheck,
  preparing: Loader2,
  legacy: Link,
  broken: FileWarning,
} as const;

const TONES = {
  // The normal state stays quiet: it is on every healthy row, and a green badge
  // per row would shout the uneventful case at a farm-sized queue.
  ready: 'text-bambu-gray',
  preparing: 'text-bambu-gray animate-spin',
  legacy: 'text-yellow-600 dark:text-yellow-400',
  broken: 'text-red-600 dark:text-red-400',
} as const;

/**
 * A one-glyph mark on a queue row: does this job own the bytes it prints?
 *
 * ⚠️ **A mark, not a card.** §10 asks for a compact indicator with a tooltip and
 * explicitly no new card size or shape, so this is an inline `span` that sits in
 * a row's existing line (beside the build-plate icon, or in the auto-queue row's
 * meta line) and takes the same space an icon there already takes. Anything that
 * needed its own block would change the geometry of every queue in the farm.
 *
 * ⚠️ **Every sentence must be true of THIS row**, not of the feature in general.
 * That is why `exempt`, a server that predates the field and any unknown value
 * all render nothing, and why `held` is ignored where nothing is stored.
 */
export function QueueSourceIndicator({
  state,
  held = false,
  className = 'w-3.5 h-3.5',
  testId,
}: QueueSourceIndicatorProps) {
  const { t } = useTranslation();
  const marked = markedStorage(state);
  if (!marked) return null;

  const Icon = ICONS[marked];
  const label = t(`queueSpool.state.${marked}.label`);
  const tip = t(`queueSpool.state.${marked}.tip`);
  const holdsAFile = held && isSelfContained(state);

  return (
    <span
      role="img"
      aria-label={label}
      title={holdsAFile ? `${tip} ${t('queueSpool.heldTip')}` : tip}
      className="inline-flex items-center flex-shrink-0"
      data-testid={testId}
    >
      <Icon className={`${className} ${TONES[marked]}`} aria-hidden="true" />
    </span>
  );
}
