import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Pencil, Trash2 } from 'lucide-react';
import type { Customer } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';

interface CustomersTableProps {
  customers: Customer[];
  onEdit: (customer: Customer) => void;
  onDelete: (customer: Customer) => void;
}

const CELL = 'px-3 py-2 text-sm';
const NUM_CELL = `${CELL} text-right tabular-nums`;

/**
 * The customer list, as the server counted it.
 *
 * Every column comes straight out of `figures` — the list endpoint's own
 * grouped query — and none of it is added up here (design decision 8). The
 * list figures deliberately carry no `printed`/`ordered`: those are the detail
 * endpoint's, and the customer page is where they are shown.
 */
export function CustomersTable({ customers, onEdit, onDelete }: CustomersTableProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const canEdit = hasPermission('projects:update');
  const canDelete = hasPermission('projects:delete');

  return (
    <div className="overflow-x-auto rounded-xl border border-bambu-dark-tertiary">
      <table className="w-full">
        <thead>
          <tr className="border-b border-bambu-dark-tertiary text-left text-xs uppercase text-bambu-gray">
            <th className={CELL}>{t('customers.table.name')}</th>
            <th className={CELL}>{t('customers.table.contact')}</th>
            <th className={NUM_CELL}>{t('customers.table.orders')}</th>
            <th className={NUM_CELL}>{t('customers.table.active')}</th>
            <th className={NUM_CELL}>{t('customers.table.completed')}</th>
            <th className={NUM_CELL}>{t('customers.table.cancelled')}</th>
            <th className={NUM_CELL}>{t('customers.table.totalPrice')}</th>
            {(canEdit || canDelete) && <th className={`${CELL} text-right`}>{t('common.actions')}</th>}
          </tr>
        </thead>
        <tbody>
          {customers.map((customer) => (
            <tr key={customer.id} className="border-b border-bambu-dark-tertiary last:border-0 hover:bg-bambu-dark/40">
              <td className={CELL}>
                <Link to={`/customers/${customer.id}`} className="text-white hover:text-bambu-green font-medium">
                  {customer.name}
                </Link>
              </td>
              <td className={`${CELL} text-bambu-gray`}>{customer.contact ?? '—'}</td>
              <td className={`${NUM_CELL} text-white`}>{customer.figures.projects}</td>
              <td className={`${NUM_CELL} text-bambu-gray`}>{customer.figures.active}</td>
              <td className={`${NUM_CELL} text-bambu-gray`}>{customer.figures.completed}</td>
              <td className={`${NUM_CELL} text-bambu-gray`}>{customer.figures.cancelled}</td>
              <td className={`${NUM_CELL} text-white`}>{customer.figures.total_price.toLocaleString()}</td>
              {(canEdit || canDelete) && (
                <td className={`${CELL} text-right whitespace-nowrap`}>
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
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
