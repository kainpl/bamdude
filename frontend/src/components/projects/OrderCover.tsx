import { useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Image as ImageIcon, Loader2, Trash2, Upload } from 'lucide-react';
import { api } from '../../api/client';
import type { Order } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';

interface OrderCoverProps {
  order: Order;
  canEdit: boolean;
}

/**
 * The order's cover picture, beside the header.
 *
 * ⚠️ **The `src` carries a `&v=` counter, not a `?v=`.** The URL already comes
 * out of `getProjectCoverImageUrl` with the camera stream token on it — an
 * `<img>` cannot send an Authorization header, which is why that endpoint
 * takes a token in the query string — so a second `?` would break the request
 * rather than bust the cache. The counter exists because replacing a cover
 * keeps the same URL: without it the browser shows the old picture until the
 * tab is reloaded.
 */
export function OrderCover({ order, canEdit }: OrderCoverProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const fileInput = useRef<HTMLInputElement>(null);
  const [version, setVersion] = useState(0);

  const refresh = () => {
    setVersion((v) => v + 1);
    queryClient.invalidateQueries({ queryKey: ['project', order.id] });
    queryClient.invalidateQueries({ queryKey: ['projects'] });
  };

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
  // box otherwise, which reads as a picture that failed to load.
  if (!order.cover_image_filename && !canEdit) return <></>;

  return (
    <div className="flex items-start gap-2">
      <div className="w-32 aspect-[3/2] rounded-lg bg-bambu-dark border border-bambu-dark-tertiary overflow-hidden flex items-center justify-center flex-shrink-0">
        {order.cover_image_filename ? (
          <img
            src={`${api.getProjectCoverImageUrl(order.id)}&v=${version}`}
            alt=""
            className="w-full h-full object-cover"
          />
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
