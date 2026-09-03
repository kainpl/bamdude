import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { FolderKanban, Loader2, X } from 'lucide-react';
import { api } from '../../api/client';
import { Card, CardContent } from '../Card';
import { Button } from '../Button';
import { useToast } from '../../contexts/ToastContext';
import { OrderPicker } from '../pickers/OrderPicker';
import { OrderLinePicker } from '../pickers/OrderLinePicker';

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

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  const assign = useMutation({
    mutationFn: (target: number) => api.addArchivesToOrder(target, archiveIds, lineId),
    onSuccess: (_, target) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['project', target] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      showToast(t('archives.toast.projectUpdated'));
      onDone?.();
      onClose();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <Card className="w-full max-w-md">
        <CardContent className="p-0">
          <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
            <div className="flex items-center gap-2">
              <FolderKanban className="w-5 h-5 text-bambu-green" />
              <h2 className="text-xl font-semibold text-white">{t('archives.bulk.assignOrder.title')}</h2>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label={t('common.close')}
              className="text-bambu-gray hover:text-white transition-colors"
              disabled={assign.isPending}
            >
              <X className="w-5 h-5" />
            </button>
          </div>

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
        </CardContent>
      </Card>
    </div>
  );
}
