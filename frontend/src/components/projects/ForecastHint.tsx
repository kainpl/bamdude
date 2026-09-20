import { useTranslation } from 'react-i18next';
import { Info } from 'lucide-react';
import type { OrderForecast } from '../../api/client';

/**
 * The assumptions behind an ETA, as a hover hint — a number without them
 * reads as a promise (spec 2026-09-06, Decision 8).
 *
 * `title` lives on the wrapping `<span>`, not the icon: lucide-react types
 * its icons with `SVGProps`, which (unlike `HTMLAttributes`) has no `title`
 * attribute, so putting it on `<Info>` directly fails `tsc -b`. `aria-label`
 * is shared by both and could sit on either; it stays on the same element
 * as `title` for one accessible name, and the icon is `aria-hidden` so it
 * isn't announced a second time.
 */
export function ForecastHint({ forecast }: { forecast: Pick<OrderForecast, 'assumptions'> }) {
  const { t } = useTranslation();
  if (forecast.assumptions.length === 0) return null;
  const title = `${t('farmForecast.assumptionsTitle')} ${forecast.assumptions.map((a) => t(`farmForecast.assumptions.${a}`, a)).join(', ')}`;
  return (
    <span className="inline-flex align-text-bottom" title={title} aria-label={title}>
      <Info className="w-3.5 h-3.5 text-bambu-gray" aria-hidden="true" />
    </span>
  );
}
