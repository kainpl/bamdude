import { useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Download, Loader2, Paperclip, Trash2, Upload } from 'lucide-react';
import { api, getAuthToken } from '../../api/client';
import type { Order, ProjectAttachment } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';

interface OrderAttachmentsProps {
  order: Order;
  canEdit: boolean;
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

/**
 * Files that belong to the order but not to the print — a customer's spec, a
 * signed quote, a photo of the packed parcel.
 *
 * ⚠️ **Downloads go through `fetch`, not a bare `<a href>`.** The GET route is
 * behind `PROJECTS_READ` and this app authenticates with a bearer token, which
 * a plain link cannot carry: the link would 401 and look like a missing file.
 * The blob dance is the same one `api.downloadArchive` does for the same
 * reason.
 */
export function OrderAttachments({ order, canEdit }: OrderAttachmentsProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const fileInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);

  const attachments = order.attachments ?? [];

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['project', order.id] });

  const upload = useMutation({
    mutationFn: (file: File) => api.uploadProjectAttachment(order.id, file),
    onSuccess: refresh,
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: (filename: string) => api.deleteProjectAttachment(order.id, filename),
    onSuccess: refresh,
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const download = async (attachment: ProjectAttachment) => {
    setBusy(true);
    try {
      const token = getAuthToken();
      const response = await fetch(api.getProjectAttachmentUrl(order.id, attachment.filename), {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const url = window.URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = attachment.original_name || attachment.filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);
    } catch (e) {
      showToast((e as Error).message, 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-lg font-semibold text-white flex items-center gap-2">
          <Paperclip className="w-5 h-5" />
          {t('orders.attachments.title')}
        </h2>

        {canEdit && (
          <>
            <input
              ref={fileInput}
              type="file"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) upload.mutate(file);
                e.target.value = '';
              }}
            />
            <Button
              variant="secondary"
              size="sm"
              onClick={() => fileInput.current?.click()}
              disabled={upload.isPending}
            >
              {upload.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
              {t('orders.attachments.upload')}
            </Button>
          </>
        )}
      </div>

      {attachments.length > 0 ? (
        <ul className="space-y-2">
          {attachments.map((attachment) => (
            <li
              key={attachment.filename}
              className="flex items-center gap-3 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary px-3 py-2"
            >
              <div className="min-w-0 flex-1">
                <p className="text-sm text-white truncate">{attachment.original_name || attachment.filename}</p>
                <p className="text-xs text-bambu-gray">{formatSize(attachment.size)}</p>
              </div>
              <button
                type="button"
                onClick={() => download(attachment)}
                disabled={busy}
                title={t('common.download')}
                className="p-1.5 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors disabled:opacity-50"
              >
                <Download className="w-4 h-4" />
              </button>
              {canEdit && (
                <button
                  type="button"
                  onClick={() => remove.mutate(attachment.filename)}
                  disabled={remove.isPending}
                  title={t('orders.attachments.delete')}
                  className="p-1.5 rounded text-status-error hover:bg-bambu-dark-tertiary transition-colors disabled:opacity-50"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-bambu-gray/70 italic">{t('orders.attachments.empty')}</p>
      )}
    </section>
  );
}
