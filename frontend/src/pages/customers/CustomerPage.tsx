import { useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ChevronRight, Loader2, Pencil, Plus, Trash2 } from 'lucide-react';
import { api } from '../../api/client';
import type { OrderListItem, ProjectStatus } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { ProgressBar } from '../../components/projects/ProgressBar';
import { OrderCard } from '../../components/projects/OrderCard';
import { OrderModal } from '../../components/projects/OrderModal';
import { CustomerModal } from '../../components/customers/CustomerModal';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Button } from '../../components/Button';
import { formatMoney } from '../../utils/currency';

/** One figure, as the server counted it — this page never adds anything up. */
function Tile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-xl bg-bambu-dark-secondary border border-bambu-dark-tertiary p-3">
      <p className="text-xs text-bambu-gray">{label}</p>
      <p className="text-lg font-semibold text-white tabular-nums">{value}</p>
    </div>
  );
}

/**
 * One customer: their figures and their orders.
 *
 * The detail endpoint's `figures` is a superset of the list one — only it
 * carries `ordered`/`printed`/`total_cost`. The `'ordered' in figures` guard is
 * what keeps a list row (which has none of the three) from silently rendering
 * an empty progress bar if this component is ever handed one.
 */
export function CustomerPage() {
  const { t } = useTranslation();
  const { id: idParam } = useParams<{ id: string }>();
  const id = Number(idParam);
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [tab, setTab] = useState<ProjectStatus | 'all'>('active');
  const [editingCustomer, setEditingCustomer] = useState(false);
  const [deletingCustomer, setDeletingCustomer] = useState(false);
  const [editingOrder, setEditingOrder] = useState<OrderListItem | null | 'new'>(null);
  const [deletingOrder, setDeletingOrder] = useState<OrderListItem | null>(null);

  const { data: customer, isLoading } = useQuery({
    queryKey: ['customer', id],
    queryFn: () => api.getCustomer(id),
    enabled: Number.isFinite(id),
  });
  const { data: orders = [] } = useQuery({
    queryKey: ['projects', { customer_id: id }],
    queryFn: () => api.getOrders({ customer_id: id }),
    enabled: Number.isFinite(id),
  });
  // The app-wide currency, fetched the way every other money-showing screen
  // fetches it; `formatMoney` covers the unresolved first paint.
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });

  const counts = useMemo(
    () => ({
      active: orders.filter((o) => o.status === 'active').length,
      completed: orders.filter((o) => o.status === 'completed').length,
      cancelled: orders.filter((o) => o.status === 'cancelled').length,
      all: orders.length,
    }),
    [orders],
  );
  const visible = tab === 'all' ? orders : orders.filter((o) => o.status === tab);

  const removeCustomer = useMutation({
    mutationFn: () => api.deleteCustomer(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['customers'] });
      // The orders survive without a customer, so their cached rows are stale too.
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      showToast(t('customers.toast.deleted'));
      navigate('/customers');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const setOrderStatus = useMutation({
    mutationFn: ({ orderId, status }: { orderId: number; status: ProjectStatus }) =>
      api.updateOrder(orderId, { status }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['customer', id] });
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });
  const removeOrder = useMutation({
    mutationFn: (orderId: number) => api.deleteOrder(orderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['customer', id] });
      showToast(t('orders.toast.deleted'));
      setDeletingOrder(null);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });
  const duplicateOrder = useMutation({
    mutationFn: (orderId: number) => api.duplicateOrder(orderId),
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['customer', id] });
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
  if (!customer) {
    return <div className="p-4 md:p-6 text-bambu-gray text-sm">{t('customers.page.notFound')}</div>;
  }

  const figures = customer.figures;
  const detailed = 'ordered' in figures ? figures : null;

  const tabs: { key: ProjectStatus | 'all'; label: string; count: number }[] = [
    { key: 'active', label: t('orders.status.active'), count: counts.active },
    { key: 'completed', label: t('orders.status.completed'), count: counts.completed },
    { key: 'cancelled', label: t('orders.status.cancelled'), count: counts.cancelled },
    { key: 'all', label: t('orders.list.tabAll'), count: counts.all },
  ];

  return (
    <div className="p-4 md:p-6 space-y-6">
      <nav className="flex items-center gap-1 text-sm text-bambu-gray">
        <Link to="/customers" className="hover:text-white transition-colors">
          {t('projects.tabs.customers')}
        </Link>
        <ChevronRight className="w-4 h-4" />
        <span className="text-white">{customer.name}</span>
      </nav>

      <header className="flex items-start justify-between gap-4 flex-wrap">
        <div className="min-w-0 space-y-1">
          <h1 className="text-2xl font-semibold text-white">{customer.name}</h1>
          {customer.contact && <p className="text-sm text-bambu-gray">{customer.contact}</p>}
          {customer.notes && <p className="text-sm text-bambu-gray whitespace-pre-line">{customer.notes}</p>}
        </div>
        <div className="flex items-center gap-2">
          {hasPermission('projects:update') && (
            <Button variant="secondary" onClick={() => setEditingCustomer(true)}>
              <Pencil className="w-4 h-4" />
              {t('common.edit')}
            </Button>
          )}
          {hasPermission('projects:delete') && (
            <Button variant="secondary" onClick={() => setDeletingCustomer(true)}>
              <Trash2 className="w-4 h-4" />
              {t('common.delete')}
            </Button>
          )}
        </div>
      </header>

      <section className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(140px,1fr))]">
        <Tile label={t('customers.table.orders')} value={figures.projects} />
        <Tile label={t('customers.table.active')} value={figures.active} />
        <Tile label={t('customers.table.completed')} value={figures.completed} />
        <Tile label={t('customers.table.cancelled')} value={figures.cancelled} />
        <Tile label={t('customers.page.totalPrice')} value={formatMoney(figures.total_price, settings?.currency)} />
        {detailed && (
          <Tile label={t('customers.page.totalCost')} value={formatMoney(detailed.total_cost, settings?.currency)} />
        )}
      </section>

      {detailed && (
        <ProgressBar
          value={detailed.printed}
          max={detailed.ordered}
          label={t('customers.page.printedOfOrdered')}
          testId="customer-progress"
        />
      )}

      <section className="space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-lg font-medium text-white">{t('customers.page.orders')}</h2>
          {hasPermission('projects:create') && (
            <Button onClick={() => setEditingOrder('new')}>
              <Plus className="w-4 h-4" />
              {t('customers.page.newOrder')}
            </Button>
          )}
        </div>

        <div role="tablist" className="flex gap-1 border-b border-bambu-dark-tertiary">
          {tabs.map(({ key, label, count }) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              onClick={() => setTab(key)}
              className={`px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${
                tab === key ? 'border-bambu-green text-white' : 'border-transparent text-bambu-gray hover:text-white'
              }`}
            >
              {label} ({count})
            </button>
          ))}
        </div>

        {visible.length === 0 ? (
          <p className="text-bambu-gray text-sm">{t(`orders.list.empty.${tab}`)}</p>
        ) : (
          <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]">
            {visible.map((order) => (
              <OrderCard
                key={order.id}
                order={order}
                onEdit={setEditingOrder}
                onDuplicate={(o) => duplicateOrder.mutate(o.id)}
                onSetStatus={(o, status) => setOrderStatus.mutate({ orderId: o.id, status })}
                onDelete={setDeletingOrder}
              />
            ))}
          </div>
        )}
      </section>

      {editingCustomer && <CustomerModal customer={customer} onClose={() => setEditingCustomer(false)} />}

      {editingOrder && (
        <OrderModal
          order={editingOrder === 'new' ? null : editingOrder}
          defaultCustomerId={id}
          onClose={() => setEditingOrder(null)}
        />
      )}

      {deletingCustomer && (
        <ConfirmModal
          title={t('customers.confirm.deleteTitle')}
          message={t('customers.confirm.deleteBody')}
          variant="danger"
          isLoading={removeCustomer.isPending}
          onConfirm={() => removeCustomer.mutate()}
          onCancel={() => setDeletingCustomer(false)}
        />
      )}

      {deletingOrder && (
        <ConfirmModal
          title={t('orders.confirm.deleteTitle')}
          message={t('orders.confirm.deleteBody')}
          variant="danger"
          isLoading={removeOrder.isPending}
          onConfirm={() => removeOrder.mutate(deletingOrder.id)}
          onCancel={() => setDeletingOrder(null)}
        />
      )}
    </div>
  );
}
