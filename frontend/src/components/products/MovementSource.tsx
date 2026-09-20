import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import type { StockMovement } from '../../api/client';

/**
 * Where a movement came from: its order, or the print that made it.
 *
 * ⚠️ **The archive is text, not a link.** There is no per-archive route in this
 * app — `/archives` is a filtered list and takes `printer`, `file` and `search`
 * params, none of which addresses one row — so a link would have to invent a
 * destination. The id is what the operator searches with; the order, which does
 * have a page, is a real link.
 */
export function MovementSource({ movement }: { movement: StockMovement }) {
  const { t } = useTranslation();
  if (movement.order_id != null) {
    return (
      <Link to={`/projects/${movement.order_id}`} className="text-bambu-green hover:underline">
        {movement.order_name ?? `#${movement.order_id}`}
      </Link>
    );
  }
  if (movement.archive_id != null) {
    return <span>{t('stock.archiveRef', { n: movement.archive_id })}</span>;
  }
  return <span className="text-bambu-gray">—</span>;
}
