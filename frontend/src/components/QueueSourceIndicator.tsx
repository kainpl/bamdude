import { FileWarning, Link, Loader2, ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { QueueSourceStorage } from '../api/client';
import { markedStorage } from '../utils/queueSource';
import { formatFileSize } from '../utils/file';

interface QueueSourceIndicatorProps {
  /** The row's own `source_storage`. Absent / `exempt` renders nothing. */
  state: QueueSourceStorage | null | undefined;
  /** Size of the saved copy, where the server knows it. Tooltip only. */
  sizeBytes?: number | null;
  /**
   * This row is failed / skipped / cancelled and still holds its file.
   *
   * Adds the one sentence §10 asks for — the file is kept ON PURPOSE until the
   * row is removed — where the operator is already looking at the row that is
   * holding it. Deliberately not a "clean up everything" button: the retry is
   * the reason the bytes are still there.
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
 * Renders `null` for `exempt`, for a server that predates the field, and for an
 * unknown value — silence beats a mark that might be wrong about file safety.
 */
export function QueueSourceIndicator({
  state,
  sizeBytes,
  held = false,
  className = 'w-3.5 h-3.5',
  testId,
}: QueueSourceIndicatorProps) {
  const { t } = useTranslation();
  const marked = markedStorage(state);
  if (!marked) return null;

  const Icon = ICONS[marked];
  const label = t(`queueSpool.state.${marked}.label`);
  const tip =
    marked === 'ready' && sizeBytes != null && sizeBytes > 0
      ? t('queueSpool.state.ready.tipWithSize', { size: formatFileSize(sizeBytes) })
      : t(`queueSpool.state.${marked}.tip`);

  return (
    <span
      role="img"
      aria-label={label}
      title={held ? `${tip} ${t('queueSpool.heldTip')}` : tip}
      className="inline-flex items-center flex-shrink-0"
      data-testid={testId}
    >
      <Icon className={`${className} ${TONES[marked]}`} aria-hidden="true" />
    </span>
  );
}
