import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { api } from '../../api/client';
import type { ProjectLine, ProjectStatus } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { OrderHeader } from '../../components/projects/OrderHeader';
import { CloseSuggestionBanner } from '../../components/projects/CloseSuggestionBanner';
import { OrderFigures } from '../../components/projects/OrderFigures';
import { OrderLinesTable } from '../../components/projects/OrderLinesTable';
import { PrintPlateFromLine } from '../../components/projects/PrintPlateFromLine';
import { OrderModal } from '../../components/projects/OrderModal';
import { ConfirmModal } from '../../components/ConfirmModal';

/**
 * One order: who it is for, what it asks for, and how much of it is printed.
 *
 * The page composes sections and owns nothing but dialog state — every figure
 * comes from `GET /projects/{id}` and is shown as sent (design decision 8).
 * The `#order-plan` anchor below the lines is deliberately empty this pass:
 * it is where the plan block lands, and reserving it now keeps the section
 * order from shifting under the operator when it arrives.
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
  const [printing, setPrinting] = useState<ProjectLine | null>(null);

  const { data: order, isLoading } = useQuery({
    queryKey: ['project', id],
    queryFn: () => api.getOrder(id),
    enabled: Number.isFinite(id),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['project', id] });
    queryClient.invalidateQueries({ queryKey: ['projects'] });
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

  const duplicate = useMutation({
    mutationFn: () => api.duplicateOrder(id),
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      showToast(t('orders.toast.duplicated'));
      navigate(`/projects/${saved.id}`);
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
  if (!order) {
    return <div className="p-4 md:p-6 text-bambu-gray text-sm">{t('orders.page.notFound')}</div>;
  }

  const canEdit = hasPermission('projects:update');

  return (
    <div className="p-4 md:p-6 space-y-6">
      <OrderHeader
        order={order}
        onEdit={() => setEditing(true)}
        onDuplicate={() => duplicate.mutate()}
        onDelete={() => setDeleting(true)}
        onSetStatus={(status) => setStatus.mutate(status)}
      />

      {canEdit && <CloseSuggestionBanner order={order} onComplete={() => setStatus.mutate('completed')} />}

      <OrderFigures figures={order.figures} />

      <OrderLinesTable order={order} canEdit={canEdit} onPrintPlate={setPrinting} />

      {/* Reserved for the plan block (pass 3) — empty on purpose. */}
      <div id="order-plan" />

      {editing && <OrderModal order={order} onClose={() => setEditing(false)} />}

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

      {printing && <PrintPlateFromLine order={order} line={printing} onClose={() => setPrinting(null)} />}
    </div>
  );
}
