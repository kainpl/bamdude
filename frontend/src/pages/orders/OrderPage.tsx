import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { api } from '../../api/client';
import type { ProjectStatus } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { OrderHeader } from '../../components/projects/OrderHeader';
import { CloseSuggestionBanner } from '../../components/projects/CloseSuggestionBanner';
import { OrderFigures } from '../../components/projects/OrderFigures';
import { OrderLinesTable } from '../../components/projects/OrderLinesTable';
import { PlanBlock } from '../../components/projects/PlanBlock';
import { OrderModal } from '../../components/projects/OrderModal';
import { OrderCover } from '../../components/projects/OrderCover';
import { ProcurementChecklist } from '../../components/projects/ProcurementChecklist';
import { OrderPrints } from '../../components/projects/OrderPrints';
import { OrderQueue } from '../../components/projects/OrderQueue';
import { OrderTimeline } from '../../components/projects/OrderTimeline';
import { OrderNotes } from '../../components/projects/OrderNotes';
import { OrderAttachments } from '../../components/projects/OrderAttachments';
import { DuplicateOrderModal } from '../../components/projects/DuplicateOrderModal';
import { ConfirmModal } from '../../components/ConfirmModal';

/**
 * One order: who it is for, what it asks for, and how much of it is printed.
 *
 * The page composes sections and owns nothing but dialog state — every figure
 * comes from `GET /projects/{id}` and is shown as sent (design decision 8).
 * `PlanBlock` below the lines answers the other half: what to print next, and
 * how to send it. It owns its own query and its own what-if counts, so the
 * page hands it the order and the edit permission and nothing else.
 */
export function OrderPage() {
  const { t } = useTranslation();
  const { id: idParam } = useParams<{ id: string }>();
  const id = Number(idParam);
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [duplicating, setDuplicating] = useState(false);

  const {
    data: order,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ['project', id],
    queryFn: () => api.getOrder(id),
    enabled: Number.isFinite(id),
  });

  // ⚠️ The customer keys too. The customer page's tiles are computed from this
  // order and its siblings, so completing or deleting it moves them; with a
  // 60 s `staleTime` a key left un-invalidated is not refetched on navigation
  // for a minute, which is long enough to read a fresh grid under stale totals.
  // `['customer']` is the PREFIX — an order can move between customers, and
  // then the customer it LEFT is stale too.
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['project', id] });
    queryClient.invalidateQueries({ queryKey: ['projects'] });
    queryClient.invalidateQueries({ queryKey: ['customers'] });
    queryClient.invalidateQueries({ queryKey: ['customer'] });
  };

  const setStatus = useMutation({
    mutationFn: (status: ProjectStatus) => api.updateOrder(id, { status }),
    onSuccess: invalidate,
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteOrder(id),
    onSuccess: () => {
      invalidate();
      showToast(t('orders.toast.deleted'));
      navigate('/projects');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  if (isLoading) {
    return (
      <div className="p-4 md:p-6 flex items-center gap-2 text-bambu-gray">
        <Loader2 className="w-4 h-4 animate-spin" />
        {t('common.loading')}
      </div>
    );
  }
  // ⚠️ Data presence first, then `isError` — see the long note on ProductPage.
  // TanStack v5 flips `status` to "error" on any failed fetch, a background
  // REFETCH of a query still holding good data included, and this page
  // invalidates `['project', id]` on every mutation its sections make. An
  // `isError`-first check would throw the whole rendered order away because a
  // refetch blipped. With no data the two cases still read apart: a fetch that
  // FAILED is not an order that was deleted.
  if (!order) {
    return isError ? (
      <div className="p-4 md:p-6 text-sm text-red-500">
        {t('orders.page.loadFailed')} {(error as Error)?.message}
      </div>
    ) : (
      <div className="p-4 md:p-6 text-bambu-gray text-sm">{t('orders.page.notFound')}</div>
    );
  }

  const canEdit = hasPermission('projects:update');

  return (
    <div className="p-4 md:p-6 space-y-6">
      {/* The cover sits in the header's right column without OrderHeader
          knowing about it — the header owns the actions row, this owns the
          picture, and neither has to grow a slot for the other. */}
      <div className="flex items-start gap-4 flex-wrap">
        <div className="min-w-0 flex-1">
          <OrderHeader
            order={order}
            onEdit={() => setEditing(true)}
            onDuplicate={() => setDuplicating(true)}
            onDelete={() => setDeleting(true)}
            onSetStatus={(status) => setStatus.mutate(status)}
          />
        </div>
        <OrderCover order={order} canEdit={canEdit} />
      </div>

      {canEdit && <CloseSuggestionBanner order={order} onComplete={() => setStatus.mutate('completed')} />}

      <OrderFigures figures={order.figures} />

      <OrderLinesTable order={order} canEdit={canEdit} />

      <PlanBlock order={order} canEdit={canEdit} />

      <ProcurementChecklist order={order} canEdit={canEdit} />

      <OrderPrints order={order} canEdit={canEdit} />

      <OrderQueue orderId={order.id} />

      <OrderTimeline orderId={order.id} />

      <OrderNotes order={order} canEdit={canEdit} />

      <OrderAttachments order={order} canEdit={canEdit} />

      {editing && <OrderModal order={order} onClose={() => setEditing(false)} />}

      {duplicating && <DuplicateOrderModal order={order} onClose={() => setDuplicating(false)} />}

      {deleting && (
        <ConfirmModal
          title={t('orders.confirm.deleteTitle')}
          message={t('orders.confirm.deleteBody')}
          variant="danger"
          isLoading={remove.isPending}
          onConfirm={() => remove.mutate()}
          onCancel={() => setDeleting(false)}
        />
      )}
    </div>
  );
}
