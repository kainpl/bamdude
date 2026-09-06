import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { ClipboardList } from 'lucide-react';
import type { OrderCandidate } from '../api/client';

/** Which order a print is filed under: none, a NEW order for this batch, or an existing order's line. */
export type OrderFilingValue =
  | { kind: 'none' }
  | { kind: 'new' }
  | { kind: 'order'; projectId: number; projectLineId: number };

interface OrderFilingFieldProps {
  value: OrderFilingValue;
  onChange: (value: OrderFilingValue) => void;
  /** Ranked by the server. Rendered in the order given — never re-sorted here. */
  candidates: OrderCandidate[] | undefined;
  loading?: boolean;
  /** The submission makes ≥ 2 prints and the operator may create orders
   *  (spec 2026-09-06, Decision 6): offer «New order for this batch». */
  offerNewOrder?: boolean;
}

/** Both ids in one option value, because a line names nothing without its order. */
const optionValue = (c: OrderCandidate) => `${c.project_id}:${c.project_line_id}`;

/**
 * "Which order is this print for?", asked where the print is started.
 *
 * ⚠️ **The field renders nothing when there is nothing to offer** — no open
 * order wants this plate AND the run is not a batch a new order could be made
 * for. A picker whose only entry is "Without an order" asks a question with
 * one answer, and the dialog it sits in is already long. The same holds while
 * the list is still being fetched: a field that appears and then vanishes is
 * worse than one that appears once, late.
 *
 * ⚠️ A candidate that needs nothing more (`outstanding_prints === 0`) is
 * labelled as covered and stays selectable — printing ahead of an order is
 * legitimate, and the list already arrives with those sorted last.
 *
 * ⚠️ **One order can be here twice.** Where two of its lines both accept this
 * plate the server offers both — it refuses to GUESS between them, which is not
 * a reason to hide the choice from the person who can answer it. The line's
 * material is appended when it has one, because otherwise the two options read
 * identically. It is DATA, not a translated label: there is no i18n key here,
 * the same way the order and product names beside it have none.
 *
 * ⚠️ **«New order for this batch» (`offerNewOrder`) is a THIRD kind, not a
 * fourth candidate.** It carries no ids yet — the order is created only on
 * submit — so the value is a `kind` union rather than an id pair that would
 * have to lie about which order until then.
 */
export function OrderFilingField({ value, onChange, candidates, loading, offerNewOrder = false }: OrderFilingFieldProps) {
  const { t } = useTranslation();
  const selectId = useId();

  if (loading) return null;
  const list = candidates ?? [];
  if (list.length === 0 && !offerNewOrder) return null;

  const current = value.kind === 'order' ? `${value.projectId}:${value.projectLineId}` : value.kind === 'new' ? 'new' : '';

  return (
    <div className="mb-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg p-3">
      <label htmlFor={selectId} className="flex items-center gap-2 text-xs text-bambu-gray mb-1">
        <ClipboardList className="w-3.5 h-3.5" />
        {t('orderFiling.label')}
      </label>
      <select
        id={selectId}
        value={current}
        onChange={(e) => {
          if (e.target.value === '') return onChange({ kind: 'none' });
          if (e.target.value === 'new') return onChange({ kind: 'new' });
          const picked = list.find((c) => optionValue(c) === e.target.value);
          onChange(picked ? { kind: 'order', projectId: picked.project_id, projectLineId: picked.project_line_id } : { kind: 'none' });
        }}
        className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded text-white px-2 py-1.5 text-sm"
      >
        <option value="">{t('orderFiling.none')}</option>
        {offerNewOrder && <option value="new">{t('orderFiling.newOrder')}</option>}
        {list.map((c) => (
          <option key={optionValue(c)} value={optionValue(c)}>
            {`${c.project_name} — ${c.product_name}${c.line_material ? ` · ${c.line_material}` : ''} · ${
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
