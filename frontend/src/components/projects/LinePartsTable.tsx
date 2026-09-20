import { useTranslation } from 'react-i18next';
import type { PartFigures } from '../../api/client';

/**
 * What one order line still needs, part by part.
 *
 * Every column is a server figure (`need` / `usable` / `in_progress` /
 * `remaining` / `surplus`) — the table only decides which of them to make
 * loud. `remaining > 0` is the work left and is bold; `surplus > 0` is amber
 * because it is neither an error nor nothing: somebody printed more of a part
 * than this line asks for, which is either a spare or a plate that should have
 * been sliced smaller.
 *
 * ⚠️ **`qty_per_unit` can legitimately be 0.** Migration-converted products
 * carry parts whose per-unit count came from an old target, so a "× 0" row is
 * data, not a bug — it means the part is not counted against this line.
 */
export function LinePartsTable({ parts }: { parts: PartFigures[] }) {
  const { t } = useTranslation();

  if (parts.length === 0) {
    return <p className="text-sm text-bambu-gray py-2">{t('orders.parts.none')}</p>;
  }

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-xs text-bambu-gray text-left">
          <th className="font-normal py-1 pr-3">{t('orders.parts.name')}</th>
          <th className="font-normal py-1 pr-3">{t('orders.parts.perUnit')}</th>
          <th className="font-normal py-1 pr-3 text-right">{t('orders.parts.need')}</th>
          <th className="font-normal py-1 pr-3 text-right">{t('orders.parts.usable')}</th>
          <th className="font-normal py-1 pr-3 text-right">{t('orders.parts.inProgress')}</th>
          <th className="font-normal py-1 pr-3 text-right">{t('orders.parts.remaining')}</th>
          <th className="font-normal py-1 text-right">{t('orders.parts.surplus')}</th>
        </tr>
      </thead>
      <tbody>
        {parts.map((part) => (
          <tr key={part.part_id} className="text-white">
            <td className="py-1 pr-3">{part.name}</td>
            <td className="py-1 pr-3 text-bambu-gray tabular-nums">{`× ${part.qty_per_unit}`}</td>
            <td className="py-1 pr-3 text-right tabular-nums">{part.need}</td>
            <td className="py-1 pr-3 text-right tabular-nums">{part.usable}</td>
            <td className="py-1 pr-3 text-right tabular-nums">{part.in_progress}</td>
            <td
              data-testid={`part-${part.part_id}-remaining`}
              className={`py-1 pr-3 text-right tabular-nums ${part.remaining > 0 ? 'font-semibold' : 'text-bambu-gray'}`}
            >
              {part.remaining}
            </td>
            <td
              className={`py-1 text-right tabular-nums ${
                part.surplus > 0 ? 'text-amber-600 dark:text-amber-400' : 'text-bambu-gray'
              }`}
            >
              {part.surplus}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
