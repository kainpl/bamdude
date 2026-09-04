import { useTranslation } from 'react-i18next';
import { useOrderDetail } from '../../hooks/useOrderDetail';

const SELECT_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none';

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
    <select
      id={id}
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
