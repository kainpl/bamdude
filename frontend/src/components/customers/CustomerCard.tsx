import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import type { Customer } from '../../api/client';
import { formatMoney } from '../../utils/currency';
import { CustomerActions } from './CustomerActions';

/**
 * One customer as a card — the second view of the customers page (spec
 * projects-lists-parity, rule 10). The same light figures the table shows,
 * straight from the list endpoint's grouped query; nothing is added up here.
 */
export function CustomerCard({
  customer,
  currency,
  onEdit,
  onDelete,
}: {
  customer: Customer;
  currency?: string;
  onEdit: (customer: Customer) => void;
  onDelete: (customer: Customer) => void;
}) {
  const { t } = useTranslation();
  const { figures } = customer;
  const chip = 'inline-block px-2 py-0.5 rounded-full text-xs bg-bambu-dark text-bambu-gray';
  return (
    <div
      data-testid={`customer-${customer.id}-card`}
      className="rounded-xl bg-bambu-dark-secondary border border-bambu-dark-tertiary hover:border-bambu-green/50 p-4 space-y-2"
    >
      <div className="flex items-start justify-between gap-2">
        <Link to={`/customers/${customer.id}`} className="font-semibold text-white hover:text-bambu-green truncate">
          {customer.name}
        </Link>
        <CustomerActions customer={customer} onEdit={onEdit} onDelete={onDelete} />
      </div>
      <p className="text-xs text-bambu-gray truncate">{customer.contact ?? '—'}</p>
      <div className="flex flex-wrap gap-1.5">
        <span className="inline-block px-2 py-0.5 rounded-full text-xs bg-bambu-green/15 text-bambu-green">
          {t('customers.card.orders', { count: figures.projects })}
        </span>
        <span className={chip}>
          {t('customers.table.active')}: {figures.active}
        </span>
        <span className={chip}>
          {t('customers.table.completed')}: {figures.completed}
        </span>
        <span className={chip}>
          {t('customers.table.cancelled')}: {figures.cancelled}
        </span>
      </div>
      <p className="text-sm text-white tabular-nums">{formatMoney(figures.total_price, currency)}</p>
    </div>
  );
}
