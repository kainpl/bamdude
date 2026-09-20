import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import { selectableProjects } from '../../utils/projects';
import { Select } from '../Select';

interface OrderPickerProps {
  value: number | null;
  onChange: (id: number | null) => void;
  disabled?: boolean;
  /** Lets a caller label the control with its own `<label htmlFor>`. The
   *  picker draws no label of its own — it sits under different headings in
   *  the archive editor and the bulk action. */
  id?: string;
}

/** `<select>` over orders the user may bind to — active ones, plus whichever
 *  order is already bound (see `selectableProjects`). Shared by the archive
 *  editor and the bulk "assign to order" action. */
export function OrderPicker({ value, onChange, disabled, id }: OrderPickerProps) {
  const { t } = useTranslation();

  const { data: orders } = useQuery({
    queryKey: ['projects', {}],
    queryFn: () => api.getOrders({}),
  });

  const options = selectableProjects(orders, value != null ? [value] : null);

  return (
    <Select
      className="w-full"
      id={id}
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
      disabled={disabled}
    >
      <option value="">{t('pickers.noOrder')}</option>
      {options.map((o) => (
        <option key={o.id} value={o.id}>
          {o.name}
        </option>
      ))}
    </Select>
  );
}
