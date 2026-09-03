import { useTranslation } from 'react-i18next';
import { CheckCircle2 } from 'lucide-react';
import type { Order } from '../../api/client';
import { Button } from '../Button';

interface CloseSuggestionBannerProps {
  order: Order;
  onComplete: () => void;
}

/**
 * "Everything is printed — close the order?"
 *
 * A SUGGESTION and never an action: nothing here closes the order on its own.
 * `all_printed` says the prints are in, not that the parcel went out, and an
 * order that closed itself the moment the last plate came off the bed would
 * have to be reopened by hand on every job that still needs assembly, QC or
 * packing. So the banner asks, once, and the operator answers.
 *
 * `all_printed` is the server's own verdict (design decision 8) — this
 * component never compares `printed` with `ordered` to second-guess it.
 */
export function CloseSuggestionBanner({ order, onComplete }: CloseSuggestionBannerProps) {
  const { t } = useTranslation();

  // A closed order has nothing to suggest: `completed` is already there, and
  // `cancelled` is a decision this banner must not quietly undo.
  if (!order.figures.all_printed || order.status !== 'active') return null;

  return (
    <div
      data-testid="close-suggestion"
      className="flex items-start justify-between gap-4 flex-wrap rounded-xl border border-bambu-green/40 bg-bambu-green/10 p-4"
    >
      <div className="flex items-start gap-3 min-w-0">
        <CheckCircle2 className="w-5 h-5 text-bambu-green flex-shrink-0 mt-0.5" />
        <div className="min-w-0">
          <p className="text-white font-medium">{t('orders.close.title')}</p>
          <p className="text-sm text-bambu-gray">{t('orders.close.body')}</p>
        </div>
      </div>
      <Button data-testid="close-suggestion-complete" onClick={onComplete}>
        {t('orders.close.action')}
      </Button>
    </div>
  );
}
