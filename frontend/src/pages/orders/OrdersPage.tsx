import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Plus } from 'lucide-react';
import { api } from '../../api/client';
import type { OrderListItem, ProjectStatus } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { ProjectsTabs } from '../../components/projects/ProjectsTabs';
import { OrderCard } from '../../components/projects/OrderCard';
import { OrderModal } from '../../components/projects/OrderModal';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Button } from '../../components/Button';

const GROUP_STORAGE_KEY = 'projects.groupByCustomer';

/** `Map` (not a plain object) so the group order matches first appearance in
 *  the already-filtered list, rather than an object's own key-insertion
 *  quirks with numeric-looking names. */
function groupBy<T>(items: T[], keyFn: (item: T) => string): Map<string, T[]> {
  const groups = new Map<string, T[]>();
  for (const item of items) {
    const key = keyFn(item);
    const existing = groups.get(key);
    if (existing) existing.push(item);
    else groups.set(key, [item]);
  }
  return groups;
}

/**
 * The order list: status tabs, a customer filter and an optional grouping.
 *
 * The list is fetched ONCE without a status filter — the tab counts need
 * every status anyway, so the tabs filter the already-loaded list client-side
 * rather than firing a second request per tab.
 */
export function OrdersPage() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [tab, setTab] = useState<ProjectStatus | 'all'>('active');
  const [customerId, setCustomerId] = useState<number | null>(null);
  const [groupByCustomer, setGroupByCustomer] = useState<boolean>(() => {
    try {
      return localStorage.getItem(GROUP_STORAGE_KEY) === '1';
    } catch {
      return false;
    }
  });
  const [editing, setEditing] = useState<OrderListItem | null | 'new'>(null);
  const [deleting, setDeleting] = useState<OrderListItem | null>(null);

  const { data: orders = [], isLoading } = useQuery({
    queryKey: ['projects', { customer_id: customerId ?? undefined }],
    queryFn: () => api.getOrders(customerId != null ? { customer_id: customerId } : {}),
  });
  const { data: customers = [] } = useQuery({ queryKey: ['customers'], queryFn: api.getCustomers });

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
  const groups = groupByCustomer ? groupBy(visible, (o) => o.customer_name ?? t('orders.list.noCustomer')) : null;

  // ⚠️ The customer keys too. `CustomerListFigures` and `CustomerFigures` are
  // computed from these very orders, so every status change, deletion and
  // duplicate moves a customer tile. `['customer']` is the PREFIX, not
  // `['customer', id]` — this page does not know whose order it just touched.
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['projects'] });
    queryClient.invalidateQueries({ queryKey: ['customers'] });
    queryClient.invalidateQueries({ queryKey: ['customer'] });
  };

  const setStatus = useMutation({
    mutationFn: ({ id, status }: { id: number; status: ProjectStatus }) => api.updateOrder(id, { status }),
    onSuccess: invalidate,
    onError: (e: Error) => showToast(e.message, 'error'),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteOrder(id),
    onSuccess: () => {
      invalidate();
      showToast(t('orders.toast.deleted'));
      setDeleting(null);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });
  const duplicate = useMutation({
    mutationFn: (id: number) => api.duplicateOrder(id),
    onSuccess: (saved) => {
      invalidate();
      showToast(t('orders.toast.duplicated'));
      navigate(`/projects/${saved.id}`);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const toggleGroupByCustomer = (value: boolean) => {
    setGroupByCustomer(value);
    try {
      localStorage.setItem(GROUP_STORAGE_KEY, value ? '1' : '0');
    } catch {
      // Private browsing / storage disabled — the toggle still works this session.
    }
  };

  const tabs: { key: ProjectStatus | 'all'; label: string; count: number }[] = [
    { key: 'active', label: t('orders.status.active'), count: counts.active },
    { key: 'completed', label: t('orders.status.completed'), count: counts.completed },
    { key: 'cancelled', label: t('orders.status.cancelled'), count: counts.cancelled },
    { key: 'all', label: t('orders.list.tabAll'), count: counts.all },
  ];

  const renderCard = (order: OrderListItem) => (
    <OrderCard
      key={order.id}
      order={order}
      onEdit={setEditing}
      onDuplicate={(o) => duplicate.mutate(o.id)}
      onSetStatus={(o, status) => setStatus.mutate({ id: o.id, status })}
      onDelete={setDeleting}
    />
  );

  return (
    <div className="p-4 md:p-6">
      <ProjectsTabs />

      <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
        <h1 className="text-2xl font-semibold text-white">{t('orders.list.title')}</h1>
        {hasPermission('projects:create') && (
          <Button onClick={() => setEditing('new')}>
            <Plus className="w-4 h-4" />
            {t('orders.list.newOrder')}
          </Button>
        )}
      </div>

      <div className="flex items-center gap-4 mb-4 flex-wrap">
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

        <select
          value={customerId ?? ''}
          onChange={(e) => setCustomerId(e.target.value ? Number(e.target.value) : null)}
          className="px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none"
        >
          <option value="">{t('orders.list.customerFilterAll')}</option>
          {customers.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>

        <label className="flex items-center gap-2 text-sm text-white cursor-pointer">
          <input
            type="checkbox"
            checked={groupByCustomer}
            onChange={(e) => toggleGroupByCustomer(e.target.checked)}
            className="accent-bambu-green"
            aria-label={t('orders.list.groupByCustomer')}
          />
          {t('orders.list.groupByCustomer')}
        </label>
      </div>

      {!isLoading && visible.length === 0 && <p className="text-bambu-gray text-sm">{t(`orders.list.empty.${tab}`)}</p>}

      {groups ? (
        <div className="space-y-6">
          {[...groups.entries()].map(([customerName, group]) => (
            <section key={customerName}>
              <h2 className="text-lg font-medium text-white mb-2">{customerName}</h2>
              <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]">{group.map(renderCard)}</div>
            </section>
          ))}
        </div>
      ) : (
        <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(280px,1fr))]">{visible.map(renderCard)}</div>
      )}

      {editing && (
        <OrderModal order={editing === 'new' ? null : editing} defaultCustomerId={customerId} onClose={() => setEditing(null)} />
      )}

      {deleting && (
        <ConfirmModal
          title={t('orders.confirm.deleteTitle')}
          message={t('orders.confirm.deleteBody')}
          variant="danger"
          isLoading={remove.isPending}
          onConfirm={() => remove.mutate(deleting.id)}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
