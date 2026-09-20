import { useRef } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Image as ImageIcon, Loader2, Trash2, Upload } from 'lucide-react';
import { api } from '../../api/client';
import type { Order } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

interface OrderCoverProps {
  order: Order;
  canEdit: boolean;
}

/**
 * The order's cover picture, beside the header.
 *
 * ⚠️ **The URL is bare — the response header is the single freshness rule.**
 * Replacing a cover keeps the same URL, which is why this used to carry a
 * `?v=` counter; the endpoint now answers `Cache-Control: private, no-cache`,
 * so the browser revalidates and a second rule here could only disagree with
 * it. (The counter also had to compute its own separator, because
 * `withStreamToken` returns the URL bare while the token is still loading —
 * one more thing that is simply gone.) The orders grid card renders the same
 * bare URL.
 */
export function OrderCover({ order, canEdit }: OrderCoverProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const fileInput = useRef<HTMLInputElement>(null);

  const refresh = () => invalidateOrderViews(queryClient, { orderId: order.id });

  const upload = useMutation({
    mutationFn: (file: File) => api.uploadProjectCoverImage(order.id, file),
    onSuccess: refresh,
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteProjectCoverImage(order.id),
    onSuccess: refresh,
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const busy = upload.isPending || remove.isPending;

  // Nothing to show and nothing to do: a read-only viewer gets an empty grey
  // box otherwise, which reads as a picture that failed to load. `null`, not
  // an empty fragment — React renders both as nothing, and only one of them
  // says so.
  if (!order.cover_image_filename && !canEdit) return null;

  const coverSrc = api.getProjectCoverImageUrl(order.id);

  return (
    <div className="flex items-start gap-2">
      <div className="w-32 aspect-[3/2] rounded-lg bg-bambu-dark border border-bambu-dark-tertiary overflow-hidden flex items-center justify-center flex-shrink-0">
        {order.cover_image_filename ? (
          <img data-testid="order-cover-image" src={coverSrc} alt="" className="w-full h-full object-cover" />
        ) : (
          <ImageIcon className="w-6 h-6 text-bambu-gray" />
        )}
      </div>

      {canEdit && (
        <div className="flex flex-col gap-2">
          <input
            ref={fileInput}
            type="file"
            accept="image/jpeg,image/png,image/gif,image/webp"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) upload.mutate(file);
              e.target.value = '';
            }}
          />
          <Button variant="secondary" size="sm" onClick={() => fileInput.current?.click()} disabled={busy}>
            {upload.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
            {t('orders.cover.upload')}
          </Button>
          {order.cover_image_filename && (
            <Button variant="secondary" size="sm" onClick={() => remove.mutate()} disabled={busy}>
              <Trash2 className="w-4 h-4" />
              {t('orders.cover.remove')}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
