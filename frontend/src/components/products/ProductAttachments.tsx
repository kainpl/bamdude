import { useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Download, Loader2, Paperclip, Trash2, Upload } from 'lucide-react';
import { api, getAuthToken } from '../../api/client';
import type { AttachmentCategory, Product, ProductAttachment } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { formatFileSize } from '../../utils/file';
import { Button } from '../Button';

interface ProductAttachmentsProps {
  product: Product;
  canEdit: boolean;
}

/**
 * The three document categories, in the order the operator meets them.
 *
 * ⚠️ `pictures` is deliberately NOT here. It is the gallery, and a picture
 * listed in both places would carry two delete buttons for one file — one of
 * which would silently also be clearing the cover.
 */
const SECTIONS: AttachmentCategory[] = ['bom_docs', 'assembly', 'other'];

/** What the file picker offers per category. This is a CONVENIENCE, never the
 *  guard: the server's per-category allowlist is the only thing that decides
 *  what may land in the attachments directory, and it answers 400 for the rest. */
const ACCEPT: Record<string, string> = {
  bom_docs: '.xls,.xlsx,.pdf,.csv',
  assembly: '.pdf,.md,image/*',
  other: '',
};

/**
 * The product's documents — bill of materials, assembly guide, everything else.
 *
 * ⚠️ **Downloads go through `fetch`, not a bare `<a href>`.** The GET route is
 * behind `PROJECTS_READ` and this app authenticates with a bearer token, which
 * a plain link cannot carry: the link would 401 and the browser would save the
 * error body under the operator's filename. Same blob dance as
 * `OrderAttachments` and `api.downloadArchive`, revoke included.
 *
 * The upload posts the CATEGORY beside the file, because that is what picks the
 * extension allowlist server-side — one route, four allowlists, and no way to
 * reach the writer without passing one of them.
 */
export function ProductAttachments({ product, canEdit }: ProductAttachmentsProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [busy, setBusy] = useState(false);

  const attachments = product.attachments ?? [];

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['product', product.id] });

  const upload = useMutation({
    mutationFn: ({ file, category }: { file: File; category: AttachmentCategory }) =>
      api.uploadProductAttachment(product.id, file, category),
    onSuccess: refresh,
    // A file over the 50 MB limit answers 413 and a wrong extension 400; both
    // arrive here as the server's own sentence, which names the limit or the
    // allowlist — nothing this component could say would be more useful.
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: (filename: string) => api.deleteProductAttachment(product.id, filename),
    onSuccess: refresh,
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const download = async (attachment: ProductAttachment) => {
    setBusy(true);
    try {
      const token = getAuthToken();
      const response = await fetch(api.getProductAttachmentUrl(product.id, attachment.filename), {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      // ⚠️ Translated, not a bare `HTTP 404`. Every other refusal on this page
      // is the server's own sentence, which is already in the operator's
      // language; a status code with nothing around it is the one message here
      // that would have read as English to everybody.
      if (!response.ok) {
        showToast(t('products.attachments.downloadFailed', { status: response.status }), 'error');
        return;
      }
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
    <section className="space-y-4">
      <h2 className="text-lg font-semibold text-white flex items-center gap-2">
        <Paperclip className="w-5 h-5" />
        {t('products.attachments.title')}
      </h2>

      {SECTIONS.map((category) => (
        <CategorySection
          key={category}
          category={category}
          entries={attachments
            .filter((a) => a.category === category)
            .sort((a, b) => a.sort_order - b.sort_order)}
          canEdit={canEdit}
          busy={busy || upload.isPending}
          deleting={remove.isPending}
          onUpload={(file) => upload.mutate({ file, category })}
          onDownload={download}
          onDelete={(filename) => remove.mutate(filename)}
        />
      ))}
    </section>
  );
}

interface CategorySectionProps {
  category: AttachmentCategory;
  entries: ProductAttachment[];
  canEdit: boolean;
  busy: boolean;
  deleting: boolean;
  onUpload: (file: File) => void;
  onDownload: (attachment: ProductAttachment) => void;
  onDelete: (filename: string) => void;
}

/** One category. Rendered even when empty — an operator who cannot see that a
 *  bill of materials belongs here will not think to upload one. */
function CategorySection({
  category,
  entries,
  canEdit,
  busy,
  deleting,
  onUpload,
  onDownload,
  onDelete,
}: CategorySectionProps) {
  const { t } = useTranslation();
  const input = useRef<HTMLInputElement>(null);

  return (
    <div className="space-y-2" data-testid={`attachment-section-${category}`}>
      <div className="flex items-center justify-between gap-4">
        <h3 className="text-sm font-medium text-white">{t(`products.attachments.category.${category}`)}</h3>
        {canEdit && (
          <>
            <input
              ref={input}
              data-testid={`attachment-input-${category}`}
              type="file"
              accept={ACCEPT[category] || undefined}
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) onUpload(file);
                e.target.value = '';
              }}
            />
            <Button type="button" variant="secondary" size="sm" onClick={() => input.current?.click()} disabled={busy}>
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
              {t('products.attachments.upload')}
            </Button>
          </>
        )}
      </div>

      {entries.length > 0 ? (
        <ul className="space-y-2">
          {entries.map((attachment) => (
            <li
              key={attachment.filename}
              className="flex items-center gap-3 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary px-3 py-2"
            >
              <div className="min-w-0 flex-1">
                <p className="text-sm text-white truncate">{attachment.original_name || attachment.filename}</p>
                <p className="text-xs text-bambu-gray">
                  {formatFileSize(attachment.size)}
                  {attachment.source === '3mf' && <span> · {t('products.attachments.fromFile')}</span>}
                </p>
              </div>
              <button
                type="button"
                onClick={() => onDownload(attachment)}
                disabled={busy}
                aria-label={`${t('common.download')}: ${attachment.original_name || attachment.filename}`}
                className="p-1.5 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors disabled:opacity-50"
              >
                <Download className="w-4 h-4" />
              </button>
              {canEdit && (
                <button
                  type="button"
                  onClick={() => onDelete(attachment.filename)}
                  disabled={deleting}
                  aria-label={`${t('common.delete')}: ${attachment.original_name || attachment.filename}`}
                  className="p-1.5 rounded text-status-error hover:bg-bambu-dark-tertiary transition-colors disabled:opacity-50"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-bambu-gray/70 italic">{t('products.attachments.empty')}</p>
      )}
    </div>
  );
}
