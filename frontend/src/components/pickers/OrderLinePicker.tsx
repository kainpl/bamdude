import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';

const SELECT_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none';

interface OrderLinePickerProps {
  orderId: number | null;
  value: number | null;
  onChange: (id: number | null) => void;
  disabled?: boolean;
}

/** `<select>` over one order's lines. Disabled until an order is chosen — a
 *  line only means something in the context of its order. */
export function OrderLinePicker({ orderId, value, onChange, disabled }: OrderLinePickerProps) {
  const { t } = useTranslation();

  const { data: order } = useQuery({
    queryKey: ['project', orderId],
    queryFn: () => api.getOrder(orderId as number),
    enabled: orderId != null,
  });

  const lines = order?.lines ?? [];

  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
      disabled={disabled || orderId == null}
      className={SELECT_CLASS}
    >
      <option value="">{orderId == null ? t('pickers.chooseOrderFirst') : t('pickers.noLine')}</option>
      {lines.map((line) => (
        <option key={line.id} value={line.id}>
          {`${line.product_name} × ${line.quantity}${line.material ? ` [${line.material}]` : ''}`}
        </option>
      ))}
    </select>
  );
}
