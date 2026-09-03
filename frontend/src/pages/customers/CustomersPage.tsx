import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Plus } from 'lucide-react';
import { api } from '../../api/client';
import type { Customer } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { ProjectsTabs } from '../../components/projects/ProjectsTabs';
import { CustomersTable } from '../../components/customers/CustomersTable';
import { CustomerModal } from '../../components/customers/CustomerModal';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Button } from '../../components/Button';

/**
 * Who the orders are for. A flat list — customers have no status of their own,
 * so there is nothing here to filter by.
 */
export function CustomersPage() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [editing, setEditing] = useState<Customer | null | 'new'>(null);
  const [deleting, setDeleting] = useState<Customer | null>(null);

  // `getCustomers` takes no arguments, so it can be handed to TanStack bare —
  // the arrow wrapper is only needed for the client functions with an optional
  // params object, which would otherwise receive the query context.
  const { data: customers = [], isLoading } = useQuery({ queryKey: ['customers'], queryFn: api.getCustomers });

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteCustomer(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['customers'] });
      // Orders keep their history and lose the customer, so their rows move too.
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      showToast(t('customers.toast.deleted'));
      setDeleting(null);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <div className="p-4 md:p-6">
      <ProjectsTabs />

      <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
        <h1 className="text-2xl font-semibold text-white">{t('customers.list.title')}</h1>
        {hasPermission('projects:create') && (
          <Button onClick={() => setEditing('new')}>
            <Plus className="w-4 h-4" />
            {t('customers.list.newCustomer')}
          </Button>
        )}
      </div>

      {!isLoading && customers.length === 0 ? (
        <p className="text-bambu-gray text-sm">{t('customers.list.empty')}</p>
      ) : (
        <CustomersTable customers={customers} onEdit={setEditing} onDelete={setDeleting} />
      )}

      {editing && <CustomerModal customer={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}

      {deleting && (
        <ConfirmModal
          title={t('customers.confirm.deleteTitle')}
          message={t('customers.confirm.deleteBody')}
          variant="danger"
          isLoading={remove.isPending}
          onConfirm={() => remove.mutate(deleting.id)}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
