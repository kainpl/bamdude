import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Copy, Loader2 } from 'lucide-react';
import { api } from '../../api/client';
import type { Order } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';
import { Modal } from '../Modal';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

interface DuplicateOrderModalProps {
  order: Order;
  onClose: () => void;
}

/**
 * Copy an order's setup into a new one.
 *
 * The dialog spells the split out rather than leaving it to be discovered:
 * the lines come across, the print history and the queue stay with the
 * original. That is the whole question anybody has before pressing the button,
 * and "duplicate" does not answer it.
 *
 * Name only — the old "include children" checkbox went with sub-projects,
 * which the order model no longer has.
 */
export function DuplicateOrderModal({ order, onClose }: DuplicateOrderModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [name, setName] = useState('');

  const duplicate = useMutation({
    mutationFn: () => api.duplicateOrder(order.id, name.trim() || undefined),
    onSuccess: (created) => {
      invalidateOrderViews(queryClient, { orderId: created.id });
      showToast(t('orders.toast.duplicated'));
      onClose();
      navigate(`/projects/${created.id}`);
    },
    // The convention is the server's own message; the key is the fallback for
    // the failure that arrives without one (a dropped connection).
    onError: (e: Error) => showToast(e.message || t('orders.duplicate.failed'), 'error'),
  });

  return (
    <Modal
      onClose={onClose}
      title={t('orders.duplicate.title')}
      icon={<Copy className="w-5 h-5 text-bambu-green" />}
      size="md"
      closeDisabled={duplicate.isPending}
    >
      <div className="p-4 space-y-4">
        <div>
          <label className="block text-sm text-bambu-gray mb-1" htmlFor="duplicate-order-name">
            {t('orders.duplicate.nameLabel')}
          </label>
          <input
            id="duplicate-order-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={`${order.name} ${t('orders.duplicate.copySuffix')}`}
            disabled={duplicate.isPending}
            className="w-full px-3 py-2 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary text-white placeholder:text-bambu-gray focus:outline-none focus:border-bambu-green"
          />
        </div>

        <div className="text-sm space-y-1">
          <p className="text-bambu-gray">{t('orders.duplicate.copies')}</p>
          <p className="text-bambu-gray">{t('orders.duplicate.excludes')}</p>
        </div>
      </div>

      <div className="flex justify-end gap-2 p-4 border-t border-bambu-dark-tertiary">
        <Button variant="secondary" onClick={onClose} disabled={duplicate.isPending}>
          {t('common.cancel')}
        </Button>
        <Button onClick={() => duplicate.mutate()} disabled={duplicate.isPending}>
          {duplicate.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : t('orders.duplicate.submit')}
        </Button>
      </div>
    </Modal>
  );
}
