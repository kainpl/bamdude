import { useTranslation } from 'react-i18next';
import type { ProjectStatus } from '../../api/client';

/** Keyed by the union so a new status is a compile error, not a silent fallback. */
const COLOURS: Record<ProjectStatus, string> = {
  active: 'bg-bambu-green/20 text-bambu-green',
  completed: 'bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400',
  cancelled: 'bg-gray-200 dark:bg-gray-500/20 text-gray-600 dark:text-gray-400',
};

/** An order's lifecycle state, in the same three colours everywhere it appears. */
export function StatusBadge({ status }: { status: ProjectStatus }) {
  const { t } = useTranslation();
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium ${COLOURS[status]}`}>
      {t(`orders.status.${status}`)}
    </span>
  );
}
