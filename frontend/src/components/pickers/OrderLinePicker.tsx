import { useTranslation } from 'react-i18next';
import { useOrderDetail } from '../../hooks/useOrderDetail';
import { Select } from '../Select';

interface OrderLinePickerProps {
  orderId: number | null;
  value: number | null;
  onChange: (id: number | null) => void;
  disabled?: boolean;
  /** Lets a caller label the control with its own `<label htmlFor>` — see
   *  `OrderPicker`. */
  id?: string;
}

/** `<select>` over one order's lines. Disabled until an order is chosen — a
 *  line only means something in the context of its order. */
export function OrderLinePicker({ orderId, value, onChange, disabled, id }: OrderLinePickerProps) {
  const { t } = useTranslation();

  // Through the shared hook, never a second `useQuery` on the same key: the
  // LAST observer to mount owns a query's options, so a picker that declared
  // its own would have taken `meta: { refreshToast: true }` off the order page
  // behind it for as long as the dialog was open. See `useOrderDetail`.
  const { data: order } = useOrderDetail(orderId);

  const lines = order?.lines ?? [];

  return (
    <Select
      className="w-full"
      id={id}
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
      disabled={disabled || orderId == null}
    >
      <option value="">{orderId == null ? t('pickers.chooseOrderFirst') : t('pickers.noLine')}</option>
      {lines.map((line) => (
        <option key={line.id} value={line.id}>
          {`${line.product_name} × ${line.quantity}${line.material ? ` [${line.material}]` : ''}`}
        </option>
      ))}
    </Select>
  );
}
