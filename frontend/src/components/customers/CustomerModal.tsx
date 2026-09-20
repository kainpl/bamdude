import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type { Customer, CustomerCreate, CustomerUpdate } from '../../api/client';
import { Button } from '../Button';
import { Modal } from '../Modal';
import { useToast } from '../../contexts/ToastContext';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

const FIELD_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none';
const LABEL_CLASS = 'block text-sm font-medium text-white mb-1';

interface CustomerModalProps {
  customer?: Customer | null;
  onClose: () => void;
}

/**
 * Create/edit dialog for one customer.
 *
 * Unlike `OrderModal`, this one sends the whole record on an edit rather than
 * a diff: a customer carries the same three fields in the list response and in
 * the detail one, so there is no shape here that could blank a field the
 * dialog never showed.
 *
 * There is deliberately no `onSaved` callback: both call sites just close the
 * dialog, and the saved record reaches every list through
 * `invalidateOrderViews` below. A prop nobody passes is a second way to learn
 * the same fact, and the one that goes uncalled when somebody adds a third
 * call site.
 */
export function CustomerModal({ customer, onClose }: CustomerModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const isEdit = !!customer;

  const [name, setName] = useState(customer?.name ?? '');
  const [contact, setContact] = useState(customer?.contact ?? '');
  const [notes, setNotes] = useState(customer?.notes ?? '');

  const mutation = useMutation({
    mutationFn: () => {
      const data: CustomerCreate & CustomerUpdate = {
        name: name.trim(),
        contact: contact.trim() === '' ? null : contact.trim(),
        notes: notes.trim() === '' ? null : notes.trim(),
      };
      return customer ? api.updateCustomer(customer.id, data) : api.createCustomer(data);
    },
    onSuccess: (saved) => {
      // `OrderListItem.customer_name` is denormalised, so renaming a customer
      // restates every order card — which is why a customer save goes through
      // the same one decision as an order save.
      invalidateOrderViews(queryClient, { customerId: customer?.id ?? saved.id });
      showToast(t('customers.toast.saved'));
      onClose();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const canSubmit = name.trim() !== '' && !mutation.isPending;

  return (
    <Modal
      onClose={onClose}
      title={isEdit ? t('customers.modal.editTitle') : t('customers.modal.createTitle')}
      size="md"
      closeDisabled={mutation.isPending}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) mutation.mutate();
        }}
      >
        <div className="p-4 space-y-4">
          <div>
            <label className={LABEL_CLASS} htmlFor="customer-name">
              {t('customers.modal.name')}
            </label>
            <input
              id="customer-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={FIELD_CLASS}
              disabled={mutation.isPending}
              required
            />
          </div>

          <div>
            <label className={LABEL_CLASS} htmlFor="customer-contact">
              {t('customers.modal.contact')}
            </label>
            <input
              id="customer-contact"
              type="text"
              value={contact}
              onChange={(e) => setContact(e.target.value)}
              className={FIELD_CLASS}
              disabled={mutation.isPending}
            />
          </div>

          <div>
            <label className={LABEL_CLASS} htmlFor="customer-notes">
              {t('customers.modal.notes')}
            </label>
            <textarea
              id="customer-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className={`${FIELD_CLASS} min-h-[72px]`}
              disabled={mutation.isPending}
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 p-4 border-t border-bambu-dark-tertiary">
          <Button type="button" variant="secondary" onClick={onClose} disabled={mutation.isPending}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={!canSubmit}>
            {mutation.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : isEdit ? (
              t('customers.modal.save')
            ) : (
              t('customers.modal.create')
            )}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
