import { useId } from 'react';
import { Printer, SlidersHorizontal } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, type OrderPrinterResourceState } from '../../api/client';
import { Button } from '../Button';
import { useAuth } from '../../contexts/AuthContext';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

export interface OrderResourceDoor { orderId: number; onChanged: () => void }

function resourceSummary(data: OrderPrinterResourceState, fallback: (id: number) => string) {
  const { models, printer_ids } = data.resources;
  return [models?.join(', '), printer_ids?.map(id => data.printers.find(p => p.id === id)?.name ?? fallback(id)).join(', ')].filter(Boolean).join(' · ');
}

export function OrderPrinterResources({ orderId, editable, onConfigure, prepareChange, onChanged }: {
  orderId: number; editable: boolean; onConfigure: () => void;
  prepareChange?: () => Promise<boolean>;
  onChanged?: () => void;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const pauseHelpId = useId();
  const client = useQueryClient();
  const queryKey = ['order-printer-resources', orderId];
  const query = useQuery({ queryKey, queryFn: () => api.getOrderPrinterResources(orderId),
    enabled: hasPermission('printers:read') });
  const update = useMutation({
    mutationFn: async (includePaused: boolean) => {
      const current = query.data;
      if (!current || (prepareChange && !(await prepareChange()))) return;
      await client.cancelQueries({ queryKey });
      return api.updateOrderPrinterResources(orderId, current.token, {
        ...current.resources, include_paused_queues: includePaused,
      });
    },
    onSuccess: saved => {
      if (!saved) return;
      client.setQueryData(queryKey, saved);
      invalidateOrderViews(client, { orderId });
      onChanged?.();
    },
    onError: () => { void query.refetch(); },
  });
  if (!hasPermission('printers:read')) return null;
  const summary = query.data && resourceSummary(query.data, id => t('orderResources.printerId', { id }));
  return <section className="rounded-xl border border-bambu-green/40 bg-bambu-green/5 p-4" aria-label={t('orderResources.title')}>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="min-w-0 flex-1">
        <h3 className="flex items-center gap-2 font-semibold text-white"><Printer className="h-5 w-5 text-bambu-green" />{t('orderResources.title')}</h3>
        <p className="mt-1 break-words text-sm text-white">{query.isPending ? t('common.loading') : query.isError ? t('orderResources.loadFailed') : summary || t('orderResources.wholeFarm')}</p>
        <p className="mt-1 text-xs text-bambu-gray">{t('orderResources.summaryHint')}</p>
      </div>
      <Button onClick={onConfigure} disabled={!editable || query.isPending || query.isError || update.isPending}>
        <SlidersHorizontal className="mr-2 h-4 w-4" />{t('orderResources.configure')}
      </Button>
    </div>
    {query.data && <div className="mt-3 border-t border-bambu-green/20 pt-2">
      <label className="flex min-h-11 w-fit items-center gap-2 text-sm font-semibold text-white">
        <input type="checkbox" className="h-4 w-4 shrink-0 accent-bambu-green"
          checked={query.data.resources.include_paused_queues === true}
          aria-describedby={pauseHelpId}
          disabled={!editable || update.isPending}
          onChange={event => update.mutate(event.target.checked)} />
        {t('orderResources.includePausedQueues')}
      </label>
      <p id={pauseHelpId} className="text-xs text-bambu-gray">{t('orderResources.includePausedQueuesHelp')}</p>
      {update.isPending && <p role="status" className="mt-1 text-xs text-bambu-gray">{t('common.saving')}</p>}
      {update.isError && <p role="alert" className="mt-1 text-sm text-red-600 dark:text-red-400">{t('orderResources.failed')}</p>}
    </div>}
    {query.isError && <Button size="sm" variant="secondary" onClick={() => void query.refetch()}>{t('common.retry')}</Button>}
  </section>;
}
