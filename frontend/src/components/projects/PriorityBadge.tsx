import { useTranslation } from 'react-i18next';
import type { ProjectPriority } from '../../api/client';

/**
 * `normal` is the absence of a badge, not a grey one — most orders are normal,
 * and a badge on every card says nothing while costing a row of space. Keyed by
 * the union so a new priority is a compile error rather than a silent fallback.
 */
const COLOURS: Record<ProjectPriority, string | null> = {
  low: 'bg-gray-200 dark:bg-gray-500/20 text-gray-600 dark:text-gray-400',
  normal: null,
  high: 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400',
  urgent: 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400',
};

export function PriorityBadge({ priority }: { priority: ProjectPriority }) {
  const { t } = useTranslation();
  const colour = COLOURS[priority];
  if (!colour) return null;
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium ${colour}`}>
      {t(`orders.priority.${priority}`)}
    </span>
  );
}
