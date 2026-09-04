import { useState } from 'react';
import DOMPurify from 'dompurify';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Edit3, FileText, Loader2, Save } from 'lucide-react';
import { api } from '../../api/client';
import type { Order } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';
import { RichTextEditor } from '../RichTextEditor';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

interface OrderNotesProps {
  order: Order;
  canEdit: boolean;
}

/**
 * Free-form notes on the order — the rich-text block moved off the old project
 * page unchanged in behaviour.
 *
 * ⚠️ **Rendered through `DOMPurify.sanitize`.** The editor produces HTML and
 * the field round-trips through the API, so what comes back is not necessarily
 * what this build put there; `dangerouslySetInnerHTML` without the sanitiser
 * would make a notes field a stored-XSS surface for anyone with write access
 * to one order.
 */
export function OrderNotes({ order, canEdit }: OrderNotesProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');

  const save = useMutation({
    mutationFn: (notes: string) => api.updateOrder(order.id, { notes }),
    onSuccess: () => {
      invalidateOrderViews(queryClient, { orderId: order.id });
      setEditing(false);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-lg font-semibold text-white flex items-center gap-2">
          <FileText className="w-5 h-5" />
          {t('orders.notes.title')}
        </h2>

        {canEdit
          && (editing ? (
            <div className="flex gap-2">
              <Button variant="secondary" size="sm" onClick={() => setEditing(false)} disabled={save.isPending}>
                {t('common.cancel')}
              </Button>
              <Button size="sm" onClick={() => save.mutate(draft)} disabled={save.isPending}>
                {save.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                {t('orders.notes.save')}
              </Button>
            </div>
          ) : (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                setDraft(order.notes || '');
                setEditing(true);
              }}
            >
              <Edit3 className="w-4 h-4" />
              {t('common.edit')}
            </Button>
          ))}
      </div>

      {editing ? (
        <RichTextEditor content={draft} onChange={setDraft} placeholder={t('orders.notes.placeholder')} />
      ) : order.notes ? (
        <div
          className="prose prose-invert prose-sm max-w-none"
          dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(order.notes) }}
        />
      ) : (
        <p className="text-sm text-bambu-gray/70 italic">{t('orders.notes.empty')}</p>
      )}
    </section>
  );
}
