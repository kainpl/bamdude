import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { AlertTriangle } from 'lucide-react';
import type { PlanPartCount } from '../../api/client';

interface PlanUnsatisfiableProps {
  productId: number;
  material: string | null;
  part: PlanPartCount;
  colSpan: number;
}

/**
 * A part the line still needs that no candidate plate makes at all.
 *
 * This is not "the plan fell short" — the greedy covered everything it could,
 * and there is simply nothing to print for this part in this material yet. So
 * the row offers the two things that would change that: the product's files,
 * where a plate is linked, and the slice slot, which is **reserved and
 * disabled** (pass-3 scope: slicing a part from the plan is a later pass). The
 * disabled button is kept rather than dropped so the answer to "why can't I
 * just slice it here" is on screen instead of absent.
 */
export function PlanUnsatisfiable({ productId, material, part, colSpan }: PlanUnsatisfiableProps) {
  const { t } = useTranslation();

  return (
    <tr data-testid={`plan-unsatisfiable-${part.part_id}`} className="border-b border-bambu-dark-tertiary last:border-0">
      <td colSpan={colSpan} className="px-3 py-2">
        <div className="flex items-center gap-2 flex-wrap text-sm">
          <AlertTriangle className="w-4 h-4 text-amber-500 shrink-0" />
          <span className="text-amber-300">
            {t('orders.plan.noPlateFor', {
              part: part.name,
              material: material ?? t('orders.plan.anyMaterial'),
            })}
          </span>
          <span className="text-bambu-gray tabular-nums">{`× ${part.count}`}</span>
          <Link to={`/products/${productId}#files`} className="text-bambu-green hover:underline">
            {t('orders.plan.linkFile')}
          </Link>
          <button
            type="button"
            disabled
            title={t('orders.plan.sliceReserved')}
            className="px-2 py-1 rounded border border-bambu-dark-tertiary text-bambu-gray opacity-50 cursor-not-allowed"
          >
            {t('orders.plan.slice')}
          </button>
        </div>
      </td>
    </tr>
  );
}
