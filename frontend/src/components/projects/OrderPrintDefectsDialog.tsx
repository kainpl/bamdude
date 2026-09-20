import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type { Archive, DefectsWriteBody } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';
import { DefectsFields } from '../DefectsFields';
import { Modal } from '../Modal';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

interface OrderPrintDefectsDialogProps {
  orderId: number;
  archive: Archive;
  onClose: () => void;
}

/**
 * «Defects…» from an order's print card: the print's part rows, one counter
 * each, saved under the ORDER's permission through the order route — the list
 * this card came from never carries rows, and the operator editing an order
 * need not hold the archive permission for a print somebody else started.
 */
export function OrderPrintDefectsDialog({ orderId, archive, onClose }: OrderPrintDefectsDialogProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const { data, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['order-print-parts', orderId, archive.id],
    queryFn: () => api.getOrderPrintParts(orderId, archive.id),
  });

  const [values, setValues] = useState<Record<number, number>>({});
  const [flat, setFlat] = useState<number>(archive.defective_count ?? 0);
  // Seed from the server once; the operator's keystrokes win over a refetch.
  const [seeded, setSeeded] = useState(false);
  useEffect(() => {
    if (data && !seeded) {
      setValues(Object.fromEntries(data.parts.map((p) => [p.id, p.defective])));
      setFlat(data.defective_count);
      setSeeded(true);
    }
  }, [data, seeded]);

  const save = useMutation({
    mutationFn: () => {
      const body: DefectsWriteBody =
        data && data.parts.length > 0
          ? { parts: data.parts.map((p) => ({ id: p.id, defective: values[p.id] ?? 0 })) }
          : { defective_count: flat };
      return api.recordOrderPrintDefects(orderId, archive.id, body);
    },
    onSuccess: (result) => {
      // No ledger-refusal toast here: a print reachable through the order route
      // is filed under an order, and the shelf correction is a no-op for those
      // by construction. The refusal is reported on the printer card and in
      // Telegram, where an order-less print is graded.
      showToast(t('orders.prints.defects.saved', { count: result.defective_count }), 'success');
      invalidateOrderViews(queryClient, { orderId });
      queryClient.invalidateQueries({ queryKey: ['archive-detail', archive.id] });
      queryClient.invalidateQueries({ queryKey: ['order-print-parts', orderId, archive.id] });
      // The Archives page's defective column and the statistics page's
      // «Defects by printer» read these; without them both keep the old numbers
      // until something unrelated refetches.
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['archiveStats'] });
      queryClient.invalidateQueries({ queryKey: ['archiveAggregate'] });
      onClose();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <Modal onClose={onClose} title={t('orders.prints.defects.title')} size="sm">
      <div className="p-4 space-y-3">
        <p className="text-sm text-white truncate">{archive.print_name || archive.filename}</p>
        {data ? (
          <DefectsFields
            parts={data.parts}
            values={values}
            onChange={(id, next) => setValues((prev) => ({ ...prev, [id]: next }))}
            quantity={data.quantity}
            flat={flat}
            onFlatChange={setFlat}
          />
        ) : isError ? (
          // A failed parts fetch used to leave the dialog on «Loading…» for
          // ever, with Save disabled and Cancel the only exit. The message is
          // already translated by the API boundary — rendered, never branched on.
          <div className="space-y-2" data-testid="print-defects-error">
            <p className="text-sm text-red-400">{(error as Error)?.message}</p>
            <Button type="button" variant="secondary" onClick={() => refetch()} disabled={isFetching}>
              {t('common.retry')}
            </Button>
          </div>
        ) : (
          <p className="text-sm text-bambu-gray">{t('common.loading')}</p>
        )}
      </div>
      <div className="p-4 border-t border-bambu-dark-tertiary flex gap-3">
        <Button type="button" variant="secondary" onClick={onClose} className="flex-1">
          {t('common.cancel')}
        </Button>
        <Button
          type="button"
          onClick={() => save.mutate()}
          disabled={!data || save.isPending}
          className="flex-1"
          data-testid="print-defects-save"
        >
          {t('orders.prints.defects.save')}
        </Button>
      </div>
    </Modal>
  );
}
