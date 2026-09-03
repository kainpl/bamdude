import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { X, Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type { Order, OrderCreate, OrderListItem, OrderUpdate, ProjectPriority, ProjectStatus } from '../../api/client';
import { Card, CardContent } from '../Card';
import { Button } from '../Button';
import { CustomerPicker } from '../pickers/CustomerPicker';
import { useToast } from '../../contexts/ToastContext';

/** Same nine presets as the old project colour picker — deliberately the only
 *  part of that modal carried over into this one. */
const ORDER_COLORS = [
  '#ef4444', // red
  '#f97316', // orange
  '#eab308', // yellow
  '#22c55e', // green
  '#06b6d4', // cyan
  '#3b82f6', // blue
  '#8b5cf6', // violet
  '#ec4899', // pink
  '#6b7280', // gray
];

const FIELD_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none';
const LABEL_CLASS = 'block text-sm font-medium text-white mb-1';

interface OrderModalProps {
  order?: OrderListItem | Order | null;
  defaultCustomerId?: number | null;
  onClose: () => void;
  onSaved?: (saved: Order) => void;
}

/**
 * Create/edit dialog for one order.
 *
 * Line editing lives on the order page, not here (design decision 5) — this
 * modal only ever touches the order's own fields. An edit sends only the
 * fields that changed from what this modal was opened with: a list row lacks
 * `description`/`url`, so those diff against `null` and are never clobbered
 * by an untouched, blank textbox.
 */
export function OrderModal({ order, defaultCustomerId, onClose, onSaved }: OrderModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const isEdit = !!order;

  const initialDescription = order && 'description' in order ? (order.description ?? '') : '';
  const initialUrl = order && 'url' in order ? (order.url ?? '') : '';
  const initialColor = order?.color ?? null;
  const initialCustomerId = order?.customer_id ?? defaultCustomerId ?? null;
  const initialTags = order?.tags ?? null;
  const initialDueDate = order?.due_date ?? null;
  const initialPriority: ProjectPriority = order?.priority ?? 'normal';
  const initialPrice = order?.price ?? null;
  const initialStatus: ProjectStatus = order?.status ?? 'active';

  const [name, setName] = useState(order?.name ?? '');
  const [customerId, setCustomerId] = useState<number | null>(initialCustomerId);
  const [description, setDescription] = useState(initialDescription);
  const [color, setColor] = useState<string | null>(order ? initialColor : ORDER_COLORS[0]);
  const [tags, setTags] = useState(initialTags ?? '');
  const [dueDate, setDueDate] = useState(initialDueDate ?? '');
  const [priority, setPriority] = useState<ProjectPriority>(initialPriority);
  const [price, setPrice] = useState(initialPrice != null ? String(initialPrice) : '');
  const [url, setUrl] = useState(initialUrl);
  const [status, setStatus] = useState<ProjectStatus>(initialStatus);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const mutation = useMutation({
    mutationFn: () => {
      if (order) {
        const data: OrderUpdate = {};
        if (name.trim() !== order.name) data.name = name.trim();
        if (customerId !== initialCustomerId) data.customer_id = customerId;
        const normDescription = description.trim() === '' ? null : description.trim();
        if (normDescription !== (initialDescription === '' ? null : initialDescription)) data.description = normDescription;
        if (color !== initialColor) data.color = color;
        const normTags = tags.trim() === '' ? null : tags.trim();
        if (normTags !== initialTags) data.tags = normTags;
        const normDueDate = dueDate === '' ? null : dueDate;
        if (normDueDate !== initialDueDate) data.due_date = normDueDate;
        if (priority !== initialPriority) data.priority = priority;
        const normPrice = price.trim() === '' ? null : Number(price);
        if (normPrice !== initialPrice) data.price = normPrice;
        const normUrl = url.trim() === '' ? null : url.trim();
        if (normUrl !== (initialUrl === '' ? null : initialUrl)) data.url = normUrl;
        if (status !== initialStatus) data.status = status;
        return api.updateOrder(order.id, data);
      }
      const data: OrderCreate = {
        name: name.trim(),
        customer_id: customerId,
        description: description.trim() === '' ? null : description.trim(),
        color,
        tags: tags.trim() === '' ? null : tags.trim(),
        due_date: dueDate === '' ? null : dueDate,
        priority,
        price: price.trim() === '' ? null : Number(price),
        url: url.trim() === '' ? null : url.trim(),
      };
      return api.createOrder(data);
    },
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      if (order) queryClient.invalidateQueries({ queryKey: ['project', order.id] });
      showToast(t('orders.toast.saved'));
      onSaved?.(saved);
      onClose();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const canSubmit = name.trim() !== '' && !mutation.isPending;

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <Card className="w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <CardContent className="p-0">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (canSubmit) mutation.mutate();
            }}
          >
            <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
              <h2 className="text-xl font-semibold text-white">
                {isEdit ? t('orders.modal.editTitle') : t('orders.modal.createTitle')}
              </h2>
              <button
                type="button"
                onClick={onClose}
                className="text-bambu-gray hover:text-white transition-colors"
                disabled={mutation.isPending}
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="p-4 space-y-4">
              <div>
                <label className={LABEL_CLASS} htmlFor="order-name">
                  {t('orders.modal.name')}
                </label>
                <input
                  id="order-name"
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className={FIELD_CLASS}
                  disabled={mutation.isPending}
                  required
                />
              </div>

              <div>
                <label className={LABEL_CLASS}>{t('orders.modal.customer')}</label>
                <CustomerPicker value={customerId} onChange={setCustomerId} disabled={mutation.isPending} allowCreate />
              </div>

              <div>
                <label className={LABEL_CLASS} htmlFor="order-description">
                  {t('orders.modal.description')}
                </label>
                <textarea
                  id="order-description"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  className={`${FIELD_CLASS} min-h-[72px]`}
                  disabled={mutation.isPending}
                />
              </div>

              <div>
                <label className={LABEL_CLASS}>{t('orders.modal.color')}</label>
                <div className="flex gap-2 flex-wrap">
                  {ORDER_COLORS.map((c) => (
                    <button
                      key={c}
                      type="button"
                      onClick={() => setColor(c)}
                      disabled={mutation.isPending}
                      className={`w-8 h-8 rounded-full transition-transform ${
                        color === c ? 'ring-2 ring-white ring-offset-2 ring-offset-bambu-dark-secondary scale-110' : ''
                      }`}
                      style={{ backgroundColor: c }}
                    />
                  ))}
                </div>
              </div>

              <div>
                <label className={LABEL_CLASS} htmlFor="order-tags">
                  {t('orders.modal.tags')}
                </label>
                <input
                  id="order-tags"
                  type="text"
                  value={tags}
                  onChange={(e) => setTags(e.target.value)}
                  className={FIELD_CLASS}
                  disabled={mutation.isPending}
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className={LABEL_CLASS} htmlFor="order-due-date">
                    {t('orders.modal.dueDate')}
                  </label>
                  <input
                    id="order-due-date"
                    type="date"
                    value={dueDate}
                    onChange={(e) => setDueDate(e.target.value)}
                    className={FIELD_CLASS}
                    disabled={mutation.isPending}
                  />
                </div>
                <div>
                  <label className={LABEL_CLASS} htmlFor="order-priority">
                    {t('orders.modal.priority')}
                  </label>
                  <select
                    id="order-priority"
                    value={priority}
                    onChange={(e) => setPriority(e.target.value as ProjectPriority)}
                    className={FIELD_CLASS}
                    disabled={mutation.isPending}
                  >
                    {(['low', 'normal', 'high', 'urgent'] as const).map((p) => (
                      <option key={p} value={p}>
                        {t(`orders.priority.${p}`)}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className={LABEL_CLASS} htmlFor="order-price">
                    {t('orders.modal.price')}
                  </label>
                  <input
                    id="order-price"
                    type="number"
                    min="0"
                    value={price}
                    onChange={(e) => setPrice(e.target.value)}
                    className={FIELD_CLASS}
                    disabled={mutation.isPending}
                  />
                </div>
                {isEdit && (
                  <div>
                    <label className={LABEL_CLASS} htmlFor="order-status">
                      {t('orders.modal.status')}
                    </label>
                    <select
                      id="order-status"
                      value={status}
                      onChange={(e) => setStatus(e.target.value as ProjectStatus)}
                      className={FIELD_CLASS}
                      disabled={mutation.isPending}
                    >
                      {(['active', 'completed', 'cancelled'] as const).map((s) => (
                        <option key={s} value={s}>
                          {t(`orders.status.${s}`)}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
              </div>

              <div>
                <label className={LABEL_CLASS} htmlFor="order-url">
                  {t('orders.modal.url')}
                </label>
                <input
                  id="order-url"
                  type="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  className={FIELD_CLASS}
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
                  t('orders.modal.save')
                ) : (
                  t('orders.modal.create')
                )}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
