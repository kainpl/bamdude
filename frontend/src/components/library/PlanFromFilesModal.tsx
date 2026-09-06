import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { ClipboardList, Loader2, X } from 'lucide-react';
import { api } from '../../api/client';
import type { Order, PartsPreview } from '../../api/client';
import { Button } from '../Button';
import { PlanBlock } from '../projects/PlanBlock';
import { useToast } from '../../contexts/ToastContext';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

interface PlanFromFilesModalProps {
  fileIds: number[];
  onClose: () => void;
}

/** `lamp.gcode.3mf` → `lamp`; the server derives the same stem for its own names. */
function stem(filename: string): string {
  return filename.replace(/\.gcode\.3mf$/i, '').replace(/\.3mf$/i, '').replace(/\.gcode$/i, '');
}

/**
 * «Розрахувати»: N library files → parts and targets → an order → the plan.
 *
 * Step 1 asks the server what the files make (`parts-preview`, read-only) and
 * takes targets per part — or, when exactly one catalogue product links every
 * file, a number of units of it. «Calculate» creates product + order in one
 * request (spec 2026-09-06, Decision 1) and step 2 is the order page's own
 * `PlanBlock` in its dialog variant: nothing about planning is re-implemented
 * here. «Cancel» on step 2 deletes the order (the adhoc product goes with it);
 * «Keep the order» closes and leaves it for later; after an enqueue the only
 * way out is «Close».
 */
export function PlanFromFilesModal({ fileIds, onClose }: PlanFromFilesModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const preview = useQuery<PartsPreview>({
    queryKey: ['parts-preview', fileIds],
    queryFn: () => api.previewPartsOfFiles(fileIds),
    retry: false,
  });

  const [targets, setTargets] = useState<Record<string, string>>({});
  const [useCatalog, setUseCatalog] = useState(true);
  const [units, setUnits] = useState('1');
  const [nameEdited, setNameEdited] = useState<string | null>(null);
  const [orderId, setOrderId] = useState<number | null>(null);
  const [enqueued, setEnqueued] = useState(false);

  const defaultName = preview.data?.files[0] ? stem(preview.data.files[0].filename) : '';
  const name = nameEdited ?? defaultName;
  const catalog = preview.data?.catalog_product ?? null;
  const catalogMode = catalog !== null && useCatalog;

  const numericTargets = useMemo(() => {
    const out: Record<string, number> = {};
    for (const [key, raw] of Object.entries(targets)) {
      const n = Number.parseInt(raw, 10);
      if (Number.isFinite(n) && n > 0) out[key] = n;
    }
    return out;
  }, [targets]);
  const unitsNumber = Number.parseInt(units, 10);
  const canCalculate =
    name.trim().length > 0 &&
    (catalogMode ? Number.isFinite(unitsNumber) && unitsNumber > 0 : Object.keys(numericTargets).length > 0);

  const create = useMutation({
    mutationFn: () =>
      catalogMode && catalog
        ? api.createOrderFromFiles({ kind: 'catalog', name: name.trim(), product_id: catalog.id, file_ids: fileIds, quantity: unitsNumber })
        : api.createOrderFromFiles({ kind: 'job', name: name.trim(), file_ids: fileIds, targets: numericTargets }),
    onSuccess: (order: Order) => {
      queryClient.setQueryData(['project', order.id], order);
      invalidateOrderViews(queryClient);
      queryClient.invalidateQueries({ queryKey: ['products'] });
      setOrderId(order.id);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const order = useQuery<Order>({
    queryKey: ['project', orderId],
    queryFn: () => api.getOrder(orderId as number),
    enabled: orderId !== null,
  });

  const cancel = useMutation({
    mutationFn: () => api.deleteOrder(orderId as number),
    onSuccess: () => {
      invalidateOrderViews(queryClient);
      queryClient.invalidateQueries({ queryKey: ['products'] });
      onClose();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const finish = (key: 'created' | 'queued') => {
    showToast(t(`orders.fromFiles.${key}`), 'success');
    onClose();
  };

  const plateLabel = (plateIndex: number) =>
    plateIndex === 0 ? t('orders.fromFiles.wholeFile') : t('orders.fromFiles.plate', { n: plateIndex });
  const fileName = (id: number) => preview.data?.files.find((f) => f.id === id)?.filename ?? `#${id}`;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true">
      <div className="w-full max-w-3xl max-h-[90vh] overflow-y-auto rounded-xl bg-bambu-dark-secondary border border-bambu-dark-tertiary">
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white flex items-center gap-2">
            <ClipboardList className="w-5 h-5" />
            {t('orders.fromFiles.title')}
            <span className="text-sm text-bambu-gray font-normal">
              · {orderId === null ? t('orders.fromFiles.stepParts') : t('orders.fromFiles.stepPlan')}
            </span>
          </h2>
          <button type="button" onClick={onClose} className="text-bambu-gray hover:text-white" aria-label={t('common.close')}>
            <X className="w-5 h-5" />
          </button>
        </div>

        {orderId === null ? (
          <div className="p-4 space-y-4">
            {preview.isLoading && <Loader2 className="w-5 h-5 animate-spin text-bambu-gray" />}
            {preview.isError && (
              <p className="text-sm text-red-400">{t('orders.fromFiles.loadFailed')} {(preview.error as Error).message}</p>
            )}
            {preview.data && (
              <>
                <div>
                  <p className="text-xs text-bambu-gray mb-1">{t('orders.fromFiles.files')}</p>
                  <ul className="text-sm text-white space-y-0.5">
                    {preview.data.files.map((f) => (
                      <li key={f.id}>
                        {f.filename}
                        {f.sliced_for_model && <span className="text-bambu-gray"> · {f.sliced_for_model}</span>}
                        {f.plates.length === 0 && <span className="text-amber-400"> · {t('orders.fromFiles.noMetadata')}</span>}
                        {f.plates.some((p) => !p.sliced) && <span className="text-amber-400"> · {t('orders.fromFiles.notSliced')}</span>}
                      </li>
                    ))}
                  </ul>
                </div>

                <label className="block">
                  <span className="text-xs text-bambu-gray">{t('orders.fromFiles.name')}</span>
                  <input
                    value={name}
                    onChange={(e) => setNameEdited(e.target.value)}
                    className="mt-1 w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-sm text-white"
                  />
                </label>

                {catalog && (
                  <label className="flex items-center gap-2 text-sm text-white cursor-pointer">
                    <input type="checkbox" checked={useCatalog} onChange={(e) => setUseCatalog(e.target.checked)} className="accent-bambu-green" />
                    {t('orders.fromFiles.useProduct', { name: catalog.name })}
                  </label>
                )}

                {catalogMode && catalog ? (
                  <div className="space-y-2">
                    <p className="text-xs text-bambu-gray">{t('orders.fromFiles.kit')}</p>
                    <ul className="text-sm text-white">
                      {catalog.parts.map((p) => (
                        <li key={p.id}>
                          {p.name} × {p.qty_per_unit}
                        </li>
                      ))}
                    </ul>
                    <label className="block max-w-[10rem]">
                      <span className="text-xs text-bambu-gray">{t('orders.fromFiles.units')}</span>
                      <input
                        type="number"
                        min={1}
                        value={units}
                        onChange={(e) => setUnits(e.target.value)}
                        className="mt-1 w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-sm text-white"
                      />
                    </label>
                  </div>
                ) : (
                  <table className="w-full text-sm">
                    <thead className="text-xs text-bambu-gray text-left">
                      <tr>
                        <th className="font-normal p-2">{t('orders.fromFiles.part')}</th>
                        <th className="font-normal p-2 w-32">{t('orders.fromFiles.targets')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.data.parts.map((part) => (
                        <tr key={part.name_key} className="border-t border-bambu-dark-tertiary align-top">
                          <td className="p-2">
                            <p className="text-white">{part.name}</p>
                            <ul className="text-xs text-bambu-gray">
                              {part.yields.map((y) => (
                                <li key={`${y.library_file_id}-${y.plate_index}`}>
                                  {t('orders.fromFiles.origin', { file: fileName(y.library_file_id), plate: plateLabel(y.plate_index), count: y.count })}
                                </li>
                              ))}
                            </ul>
                          </td>
                          <td className="p-2">
                            <input
                              type="number"
                              min={0}
                              aria-label={part.name}
                              value={targets[part.name_key] ?? ''}
                              onChange={(e) => setTargets((prev) => ({ ...prev, [part.name_key]: e.target.value }))}
                              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-white"
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}

                <div className="flex justify-end gap-2">
                  <Button variant="secondary" onClick={onClose}>{t('orders.fromFiles.cancel')}</Button>
                  <Button
                    onClick={() => create.mutate()}
                    disabled={!canCalculate || create.isPending}
                    title={!canCalculate && !catalogMode ? t('orders.fromFiles.noTargets') : undefined}
                  >
                    {create.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                    {t('orders.fromFiles.calculate')}
                  </Button>
                </div>
              </>
            )}
          </div>
        ) : (
          <div className="p-4 space-y-4">
            {order.data ? (
              <PlanBlock order={order.data} canEdit variant="dialog" onEnqueued={() => setEnqueued(true)} />
            ) : (
              <Loader2 className="w-5 h-5 animate-spin text-bambu-gray" />
            )}
            <p className="text-xs text-bambu-gray">{t('orders.fromFiles.hoursNote')}</p>
            <div className="flex justify-between items-center gap-2 flex-wrap">
              <Link to={`/projects/${orderId}`} className="text-sm text-bambu-green hover:underline" onClick={onClose}>
                {t('orders.fromFiles.openOrder')}
              </Link>
              <div className="flex gap-2">
                {enqueued ? (
                  <Button onClick={() => finish('queued')}>{t('orders.fromFiles.close')}</Button>
                ) : (
                  <>
                    <Button variant="secondary" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
                      {t('orders.fromFiles.cancel')}
                    </Button>
                    <Button onClick={() => finish('created')}>{t('orders.fromFiles.keepOrder')}</Button>
                  </>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
