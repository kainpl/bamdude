import { useTranslation } from 'react-i18next';
import { Pencil, Trash2 } from 'lucide-react';
import type { Customer } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';

/**
 * Edit / delete for one customer — the table row and the card both render
 * THIS, so the two views can never offer different actions for one customer.
 * Renders nothing for a user who may do neither.
 */
export function CustomerActions({
  customer,
  onEdit,
  onDelete,
}: {
  customer: Customer;
  onEdit: (customer: Customer) => void;
  onDelete: (customer: Customer) => void;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const canEdit = hasPermission('projects:update');
  const canDelete = hasPermission('projects:delete');
  if (!canEdit && !canDelete) return null;
  return (
    <span className="inline-flex items-center whitespace-nowrap">
      {canEdit && (
        <button
          type="button"
          onClick={() => onEdit(customer)}
          aria-label={t('common.edit')}
          className="p-1.5 rounded-lg text-bambu-gray hover:text-white hover:bg-bambu-dark transition-colors"
        >
          <Pencil className="w-4 h-4" />
        </button>
      )}
      {canDelete && (
        <button
          type="button"
          onClick={() => onDelete(customer)}
          aria-label={t('common.delete')}
          className="p-1.5 rounded-lg text-bambu-gray hover:text-red-500 hover:bg-bambu-dark transition-colors"
        >
          <Trash2 className="w-4 h-4" />
        </button>
      )}
    </span>
  );
}
