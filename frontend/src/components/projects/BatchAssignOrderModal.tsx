import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { FolderKanban, Loader2 } from 'lucide-react';
import { api } from '../../api/client';
import { Button } from '../Button';
import { Modal } from '../Modal';
import { useToast } from '../../contexts/ToastContext';
import { OrderPicker } from '../pickers/OrderPicker';
import { OrderLinePicker } from '../pickers/OrderLinePicker';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

interface BatchAssignOrderModalProps {
  archiveIds: number[];
  onClose: () => void;
  onDone?: () => void;
}

/**
 * File a selection of archives under one order, optionally under one line.
 *
 * Replaces `BatchProjectModal`, which hardcoded its English and hand-rolled a
 * project list beside the shared rule. The pickers are the same two the
 * archive editor uses, so "which orders may be offered" is answered in one
 * place — and changing the order clears the line here for the same reason it
 * does there: the server refuses a line from another order.
 */
export function BatchAssignOrderModal({ archiveIds, onClose, onDone }: BatchAssignOrderModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [orderId, setOrderId] = useState<number | null>(null);
  const [lineId, setLineId] = useState<number | null>(null);

  const assign = useMutation({
    mutationFn: (target: number) => api.addArchivesToOrder(target, archiveIds, lineId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      // Prefixes, not the picked order alone — a selection can be pulled out
      // of several other orders, and several customers, in one go. The set
      // itself is decided in `utils/queryInvalidation.ts`.
      invalidateOrderViews(queryClient);
      showToast(t('archives.toast.orderUpdated'));
      onDone?.();
      onClose();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <Modal
      onClose={onClose}
      title={t('archives.bulk.assignOrder.title')}
      icon={<FolderKanban className="w-5 h-5 text-bambu-green" />}
      size="md"
      closeDisabled={assign.isPending}
    >
      <div className="p-4 space-y-4">
        <p className="text-sm text-bambu-gray">
          {t('archives.bulk.assignOrder.description', { count: archiveIds.length })}
        </p>

        <div>
          <label htmlFor="batch-assign-order" className="block text-sm text-bambu-gray mb-1">
            {t('archives.bulk.assignOrder.order')}
          </label>
          <OrderPicker
            id="batch-assign-order"
            value={orderId}
            onChange={(next) => {
              setOrderId(next);
              setLineId(null);
            }}
            disabled={assign.isPending}
          />
        </div>

        <div>
          <label htmlFor="batch-assign-line" className="block text-sm text-bambu-gray mb-1">
            {t('archives.bulk.assignOrder.line')}
          </label>
          <OrderLinePicker
            id="batch-assign-line"
            orderId={orderId}
            value={lineId}
            onChange={setLineId}
            disabled={assign.isPending}
          />
        </div>
      </div>

      <div className="flex gap-3 p-4 border-t border-bambu-dark-tertiary">
        <Button variant="secondary" onClick={onClose} className="flex-1" disabled={assign.isPending}>
          {t('common.cancel')}
        </Button>
        <Button
          onClick={() => orderId != null && assign.mutate(orderId)}
          className="flex-1"
          disabled={orderId == null || assign.isPending}
        >
          {assign.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
          {t('archives.bulk.assignOrder.assign')}
        </Button>
      </div>
    </Modal>
  );
}
