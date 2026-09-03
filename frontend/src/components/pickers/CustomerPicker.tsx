import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { api } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';

const FIELD_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none';
/** Sentinel `<option>` value that swaps the select for the inline create form. */
const NEW_CUSTOMER = '__new__';

interface CustomerPickerProps {
  value: number | null;
  onChange: (id: number | null) => void;
  disabled?: boolean;
  allowCreate?: boolean;
}

/** `<select>` over customers, with a "new customer…" option that swaps to an
 *  inline name field + create button rather than opening a separate modal. */
export function CustomerPicker({ value, onChange, disabled, allowCreate }: CustomerPickerProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');

  const { data: customers } = useQuery({ queryKey: ['customers'], queryFn: api.getCustomers });

  const createMutation = useMutation({
    mutationFn: (customerName: string) => api.createCustomer({ name: customerName }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ['customers'] });
      onChange(created.id);
      setCreating(false);
      setName('');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const cancelCreate = () => {
    setCreating(false);
    setName('');
  };

  if (creating) {
    return (
      <div className="flex gap-2">
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          // ⚠️ `stopPropagation` is the point: the modals this picker lives in
          // close themselves on a `window` keydown, so an unguarded Escape
          // here would throw away the whole order the user was editing
          // instead of stepping back out of the create field.
          onKeyDown={(e) => {
            if (e.key !== 'Escape') return;
            e.stopPropagation();
            cancelCreate();
          }}
          placeholder={t('pickers.newCustomerName')}
          className={FIELD_CLASS}
          disabled={disabled}
          autoFocus
        />
        <button
          type="button"
          onClick={() => name.trim() && createMutation.mutate(name.trim())}
          disabled={disabled || !name.trim() || createMutation.isPending}
          className="px-3 py-2 rounded-lg text-sm bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30 transition-colors whitespace-nowrap"
        >
          {t('pickers.create')}
        </button>
        {/* Picking "new customer…" by accident used to be a one-way door: the
            select was gone and only creating a customer brought it back. */}
        <button
          type="button"
          onClick={cancelCreate}
          disabled={createMutation.isPending}
          aria-label={t('pickers.cancelCreate')}
          title={t('pickers.cancelCreate')}
          className="px-2 py-2 rounded-lg text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors"
        >
          <X className="w-4 h-4" />
        </button>
      </div>
    );
  }

  return (
    <select
      value={value ?? ''}
      onChange={(e) => {
        if (e.target.value === NEW_CUSTOMER) {
          setCreating(true);
          return;
        }
        onChange(e.target.value ? Number(e.target.value) : null);
      }}
      disabled={disabled}
      className={FIELD_CLASS}
    >
      <option value="">{t('pickers.noCustomer')}</option>
      {customers?.map((c) => (
        <option key={c.id} value={c.id}>
          {c.name}
        </option>
      ))}
      {allowCreate && <option value={NEW_CUSTOMER}>{t('pickers.newCustomer')}</option>}
    </select>
  );
}
