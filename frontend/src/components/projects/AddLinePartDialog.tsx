import { useId, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, ApiError, type LinePartAdd, type ProjectLine } from '../../api/client';
import { Button } from '../Button';
import { WorkshopDialog } from '../workshop/WorkshopDialog';

const fieldClass = 'w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2 text-white';

export function AddLinePartDialog({
  orderId, line, onClose, onSaved,
}: {
  orderId: number; line: ProjectLine; onClose: () => void; onSaved: () => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const [allParts, setAllParts] = useState(false);
  const [search, setSearch] = useState('');
  const [partId, setPartId] = useState(0);
  const [quantity, setQuantity] = useState('');
  const submitRef = useRef<LinePartAdd | null>(null);
  const saving = useRef(false);
  const query = useQuery({
    queryKey: ['line-part-candidates', orderId, line.id],
    queryFn: () => api.getLinePartCandidates(orderId, line.id),
    staleTime: 0,
  });
  const data = query.data;
  const all = allParts || !data?.variant_available;
  const parts = (data?.parts ?? []).filter((part) =>
    (all || part.compatible) && part.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()),
  );
  const selected = parts.find((part) => part.id === partId && part.saved_qty === 0);
  const count = Number(quantity);
  const valid = Number.isInteger(count) && count > 0 && count <= 9999;
  const total = line.mode === 'parts' ? count : count * (data?.quantity ?? line.quantity);
  const extras = Math.ceil(total * (selected?.extra_percent ?? 0) / 100);
  const hasReservation = line.from_finished > 0 || line.from_kit_units > 0;
  const reserve = useQuery({
    queryKey: ['add-line-part-impact', orderId, line.id, data?.revision, data?.catalog_version, data?.quantity, partId, count],
    queryFn: () => api.previewLineConfiguration(orderId, line.id, {
      choices: {}, part_counts: {}, part_edits: { [partId]: count },
    }),
    enabled: hasReservation && !!selected && valid && !query.isFetching,
    retry: false,
  });
  const save = useMutation({
    mutationFn: (body: LinePartAdd) => api.addLinePart(orderId, line.id, body),
    onSuccess: onSaved,
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 409) await query.refetch();
    },
    onSettled: () => { saving.current = false; },
  });
  const pending = save.isPending || query.isFetching || (hasReservation && !reserve.isSuccess);
  const variant = line.configuration.choices.map((choice) => choice.option_name).join(' · ');

  function submit() {
    if (!data || !selected || !valid || pending || saving.current) return;
    const body = {
      part_id: selected.id,
      quantity: count,
      all_parts: all,
      expected_revision: data.revision,
      expected_quantity: data.quantity,
      catalog_version: data.catalog_version,
    };
    const old = submitRef.current;
    const same = old && Object.entries(body).every(([key, value]) => old[key as keyof typeof body] === value);
    const commandId = same ? old.command_id : Array.from(crypto.getRandomValues(new Uint32Array(4))).join('-');
    submitRef.current = { ...body, command_id: commandId };
    saving.current = true;
    save.mutate(submitRef.current);
  }

  return (
    <WorkshopDialog
      onClose={onClose}
      title={t('orders.lines.addLinePart')}
      subtitle={[line.product_name, variant].filter(Boolean).join(' · ')}
      pending={save.isPending}
      error={save.error?.message || query.error?.message || reserve.error?.message}
      footer={(
        <>
          <Button variant="secondary" onClick={onClose} disabled={save.isPending}>{t('common.cancel')}</Button>
          <Button type="submit" form={id} disabled={!selected || !valid || pending}>{t('common.add')}</Button>
        </>
      )}
    >
      <form id={id} className="space-y-3" onSubmit={(event) => { event.preventDefault(); submit(); }}>
        <fieldset className="space-y-3" disabled={save.isPending}>
          {line.mode === 'product' && data?.variant_available && (
            <div className="flex flex-wrap gap-x-4 gap-y-2 text-sm">
              <label className="flex items-center gap-2">
                <input type="radio" name={`${id}-filter`} checked={!all} onChange={() => setAllParts(false)} />
                {t('orders.lines.partsForVariant')}
              </label>
              <label className="flex items-center gap-2">
                <input type="radio" name={`${id}-filter`} checked={all} onChange={() => setAllParts(true)} />
                {t('orders.lines.allProductParts')}
              </label>
            </div>
          )}
          {line.mode === 'product' && data && !data.variant_available && (
            <p className="text-sm text-bambu-gray">{t('orders.lines.currentVariantUnavailable')}</p>
          )}
          <label className="block space-y-1 text-sm">
            <span>{t('orders.lines.findPart')}</span>
            <input className={fieldClass} type="search" value={search} onChange={(e) => setSearch(e.target.value)} />
          </label>
          <label className="block space-y-1 text-sm">
            <span>{t('orders.lines.choosePart')}</span>
            <select className={fieldClass} value={selected?.id ?? 0} onChange={(e) => {
              const part = parts.find((p) => p.id === Number(e.target.value));
              setPartId(part?.id ?? 0);
              setQuantity(part ? String(part.suggested_qty) : '');
            }}>
              <option value={0}>{t(query.isFetching ? 'common.loading' : parts.length ? 'orders.lines.choosePart' : 'orders.lines.noMatchingParts')}</option>
              {parts.map((part) => (
                <option key={part.id} value={part.id} disabled={part.saved_qty > 0}>
                  {part.name}
                  {part.saved_qty > 0 ? ` · ${t('orders.lines.partAlreadyInLine', { count: part.saved_qty })}` : ''}
                  {all && data?.variant_available && !part.compatible ? ` · ${t('orders.lines.otherVariant')}` : ''}
                </option>
              ))}
            </select>
          </label>
          <label className="block space-y-1 text-sm">
            <span>{t(line.mode === 'parts' ? 'orders.lines.totalParts' : 'orders.lines.partsPerUnit')}</span>
            <input className={fieldClass} type="number" min={1} max={9999} step={1} value={quantity}
              onChange={(e) => setQuantity(e.target.value)} disabled={!selected} />
          </label>
          {selected && valid && line.mode === 'product' && (
            <p role="status" className="text-sm tabular-nums">
              {t('orders.lines.addPartTotal', { per: count, units: data?.quantity, total })}
              {extras > 0 && ` · ${t('orders.lines.addPartExtras', { count: extras })}`}
            </p>
          )}
          {reserve.data && selected && valid && (
            <div className="space-y-1 text-sm text-bambu-gray">
              {reserve.data.reserved_after != null && reserve.data.reserved_before !== reserve.data.reserved_after && (
                <p>{t('orders.lineConfig.impact.reserve', {
                  before: reserve.data.reserved_before, after: reserve.data.reserved_after,
                })}</p>
              )}
              {reserve.data.finished_after != null && reserve.data.finished_before !== reserve.data.finished_after && (
                <p>{t('orders.lineConfig.impact.ready', {
                  before: reserve.data.finished_before, after: reserve.data.finished_after,
                })}</p>
              )}
            </div>
          )}
        </fieldset>
      </form>
    </WorkshopDialog>
  );
}
