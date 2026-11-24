import { useId, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, ApiError, type LinePartAdd, type ProjectLine } from '../../api/client';
import { Button } from '../Button';
import { Select } from '../Select';
import { WorkshopDialog } from '../workshop/WorkshopDialog';

const fieldClass = 'w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2 text-white';

// Use the decimal spelling from the catalog, as the server does. A binary
// product just above an integer would otherwise round up by one extra part.
function extraParts(total: number, percent: number): number {
  const [mantissa, exponent = '0'] = String(percent).split('e');
  const [whole, fraction = ''] = mantissa.split('.');
  const scale = fraction.length - Number(exponent) + 2;
  const digits = BigInt(whole + fraction);
  const numerator = BigInt(total) * digits * 10n ** BigInt(Math.max(0, -scale));
  const denominator = 10n ** BigInt(Math.max(0, scale));
  return Number((numerator + denominator - 1n) / denominator);
}

export function AddLinePartDialog({
  orderId, line, onClose, onSaved,
}: {
  orderId: number;
  line: Pick<ProjectLine, 'id' | 'mode' | 'product_name' | 'quantity'>;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const [allParts, setAllParts] = useState(false);
  const [search, setSearch] = useState('');
  const [draft, setDraft] = useState<{ identity: string; quantity: string } | null>(null);
  const previousSubmit = useRef<{ body: string; commandId: string } | null>(null);
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
  const selected = draft && parts.find((part) => part.part_identity === draft.identity && part.saved_qty === 0);
  const quantity = selected ? draft?.quantity ?? '' : '';
  const count = Number(quantity);
  const valid = Number.isInteger(count) && count > 0 && count <= 9999;
  const total = line.mode === 'parts' ? count : count * (data?.quantity ?? line.quantity);
  const extras = valid ? extraParts(total, selected?.extra_percent ?? 0) : 0;
  const reserve = useQuery({
    // A fresh candidates read needs a fresh stock preview even when its contents are identical.
    queryKey: ['add-line-part-impact', orderId, line.id, query.dataUpdatedAt, data?.revision, data?.catalog_version, data?.quantity, selected?.part_identity, count],
    queryFn: () => api.previewLineConfiguration(orderId, line.id, {
      choices: {}, part_counts: {},
      catalog_part_edits: selected ? { [selected.part_identity]: count } : {},
      expected_revision: data?.revision,
    }),
    enabled: !!selected && valid && query.isSuccess && !query.isFetching,
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
  const pending = save.isPending || query.isFetching || !query.isSuccess || !reserve.isSuccess || reserve.isFetching;
  const variant = data?.variant_names?.join(' · ');

  function submit() {
    if (!data || !selected || !valid || !reserve.data || pending || saving.current) return;
    const body = {
      part_id: selected.id,
      part_identity: selected.part_identity,
      quantity: count,
      all_parts: all,
      expected_revision: data.revision,
      expected_quantity: data.quantity,
      catalog_version: data.catalog_version,
      reservation_review: {
        reserved_before: reserve.data.reserved_before,
        reserved_after: reserve.data.reserved_after,
        finished_before: reserve.data.finished_before,
        finished_after: reserve.data.finished_after,
        part_reservation_version: reserve.data.part_reservation_version,
      },
    };
    const serialized = JSON.stringify(body);
    const old = previousSubmit.current;
    const commandId = old?.body === serialized ? old.commandId : Array.from(crypto.getRandomValues(new Uint32Array(4))).join('-');
    previousSubmit.current = { body: serialized, commandId };
    saving.current = true;
    save.mutate({ ...body, command_id: commandId });
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
            <Select className="block w-full" value={selected?.id ?? 0} onChange={(e) => {
              const part = parts.find((p) => p.id === Number(e.target.value));
              setDraft(part ? { identity: part.part_identity, quantity: String(part.suggested_qty) } : null);
              save.reset();
            }}>
              <option value={0}>{t(query.isFetching ? 'common.loading' : parts.length ? 'orders.lines.choosePart' : 'orders.lines.noMatchingParts')}</option>
              {parts.map((part) => (
                <option key={part.part_identity} value={part.id} disabled={part.saved_qty > 0}>
                  {part.name}
                  {part.saved_qty > 0 ? ` · ${t('orders.lines.partAlreadyInLine', { count: part.saved_qty })}` : ''}
                  {all && data?.variant_available && !part.compatible ? ` · ${t('orders.lines.otherVariant')}` : ''}
                </option>
              ))}
            </Select>
          </label>
          <label className="block space-y-1 text-sm">
            <span>{t(line.mode === 'parts' ? 'orders.lines.totalParts' : 'orders.lines.partsPerUnit')}</span>
            <input className={fieldClass} type="number" min={1} max={9999} step={1} value={quantity}
              onChange={(e) => {
                const value = e.target.value;
                setDraft((previous) => previous ? { ...previous, quantity: value } : null);
              }} disabled={!selected} />
          </label>
          {selected && valid && line.mode === 'product' && (
            <p role="status" className="text-sm tabular-nums">
              {t('orders.lines.addPartTotal', { per: count, units: data?.quantity, total })}
              {extras > 0 && ` · ${t('orders.lines.addPartExtras', { count: extras })}`}
            </p>
          )}
          {selected && valid && reserve.isFetching && (
            <p role="status" className="text-sm text-bambu-gray">{t('orders.lineConfig.checking')}</p>
          )}
          {(query.isError || reserve.isError) && (
            <Button type="button" size="sm" variant="ghost" onClick={() => query.refetch()}>
              {t('common.retry')}
            </Button>
          )}
          {reserve.isSuccess && !query.isFetching && !reserve.isFetching && selected && valid && (
            <div className="space-y-1 text-sm text-bambu-gray">
              {reserve.data.reserved_after != null && reserve.data.reserved_before !== reserve.data.reserved_after && (
                <p>{t('orders.lineConfig.impact.reserve', {
                  before: reserve.data.reserved_before, after: reserve.data.reserved_after,
                })}</p>
              )}
              {reserve.data.released_parts?.map((part) => (
                <p key={part.part_id}>{t('orders.lines.releaseReservedPart', { name: part.name, quantity: part.quantity })}</p>
              ))}
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
