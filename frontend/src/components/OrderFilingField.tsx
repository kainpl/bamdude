import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { ClipboardList } from 'lucide-react';
import type { OrderCandidate } from '../api/client';

/** Which order, and which line of it, a print is filed under. */
export interface OrderFilingValue {
  projectId: number;
  projectLineId: number;
}

interface OrderFilingFieldProps {
  value: OrderFilingValue | null;
  onChange: (value: OrderFilingValue | null) => void;
  /** Ranked by the server. Rendered in the order given — never re-sorted here. */
  candidates: OrderCandidate[] | undefined;
  loading?: boolean;
}

/** Both ids in one option value, because a line names nothing without its order. */
const optionValue = (c: OrderCandidate) => `${c.project_id}:${c.project_line_id}`;

/**
 * "Which order is this print for?", asked where the print is started.
 *
 * ⚠️ **The field renders nothing when no open order wants this plate.** A
 * picker whose only entry is "Without an order" asks a question with one
 * answer, and the dialog it sits in is already long. The same holds while the
 * list is still being fetched: a field that appears and then vanishes is worse
 * than one that appears once, late.
 *
 * ⚠️ A candidate that needs nothing more (`outstanding_prints === 0`) is
 * labelled as covered and stays selectable — printing ahead of an order is
 * legitimate, and the list already arrives with those sorted last.
 */
export function OrderFilingField({ value, onChange, candidates, loading }: OrderFilingFieldProps) {
  const { t } = useTranslation();
  const selectId = useId();

  if (loading) return null;
  if (!candidates || candidates.length === 0) return null;

  return (
    <div className="mb-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg p-3">
      <label htmlFor={selectId} className="flex items-center gap-2 text-xs text-bambu-gray mb-1">
        <ClipboardList className="w-3.5 h-3.5" />
        {t('orderFiling.label')}
      </label>
      <select
        id={selectId}
        value={value ? `${value.projectId}:${value.projectLineId}` : ''}
        onChange={(e) => {
          const picked = candidates.find((c) => optionValue(c) === e.target.value);
          onChange(picked ? { projectId: picked.project_id, projectLineId: picked.project_line_id } : null);
        }}
        className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded text-white px-2 py-1.5 text-sm"
      >
        <option value="">{t('orderFiling.none')}</option>
        {candidates.map((c) => (
          <option key={optionValue(c)} value={optionValue(c)}>
            {`${c.project_name} — ${c.product_name} · ${
              c.outstanding_prints > 0
                ? t('orderFiling.stillNeeds', { count: c.outstanding_prints })
                : t('orderFiling.satisfied')
            }`}
          </option>
        ))}
      </select>
    </div>
  );
}
