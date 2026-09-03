import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import { selectableProjects } from '../../utils/projects';

const SELECT_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none';

interface OrderPickerProps {
  value: number | null;
  onChange: (id: number | null) => void;
  disabled?: boolean;
}

/** `<select>` over orders the user may bind to — active ones, plus whichever
 *  order is already bound (see `selectableProjects`). Shared by the archive
 *  editor and the bulk "assign to order" action. */
export function OrderPicker({ value, onChange, disabled }: OrderPickerProps) {
  const { t } = useTranslation();

  const { data: orders } = useQuery({
    queryKey: ['projects', {}],
    queryFn: () => api.getOrders({}),
  });

  const options = selectableProjects(orders, value != null ? [value] : null);

  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
      disabled={disabled}
      className={SELECT_CLASS}
    >
      <option value="">{t('pickers.noOrder')}</option>
      {options.map((o) => (
        <option key={o.id} value={o.id}>
          {o.name}
        </option>
      ))}
    </select>
  );
}
