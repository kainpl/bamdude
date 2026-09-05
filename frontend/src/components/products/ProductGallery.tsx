import { useEffect, useId, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, ChevronRight, ChevronUp, ChevronDown, Image, Loader2, Package, Star, Trash2, Upload, X } from 'lucide-react';
import { api } from '../../api/client';
import type { Product, ProductAttachment } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';
import { invalidateOrderViews } from '../../utils/queryInvalidation';
import { byAttachmentOrder } from './attachmentOrder';
import { useDialogFocus } from '../../hooks/useDialogFocus';

interface ProductGalleryProps {
  product: Product;
  canEdit: boolean;
  /** Appended to every `data-testid` here. The product PAGE renders this
   *  gallery and so does the card DIALOG opened over it, and two live galleries
   *  with the same testids make every `getByTestId` in a page test ambiguous —
   *  the page passes nothing, the dialog passes `-dialog`. */
  testIdSuffix?: string;
  /** The catalogue key of this gallery's own heading, which is also the section's
   *  accessible name. Same collision as `testIdSuffix`, for the people rather
   *  than the tests: while the card dialog is open two galleries are live, and
   *  "Pictures" names both of them. */
  headingKey?: string;
  /** Told whenever the lightbox opens or closes.
   *
   *  ⚠️ It exists so an enclosing DIALOG can stand down from Escape. Both
   *  listeners sit on `window`, and the dialog's is registered first (its
   *  effect runs on mount, the gallery's only once a picture is enlarged), so
   *  one Escape closed the dialog out from under the lightbox — and with it
   *  everything typed into the form behind. `ModelCardModal` has no such prop
   *  because its lightbox is its own state and it can order the two branches
   *  in a single handler; here the state lives one component down. */
  onLightboxOpenChange?: (open: boolean) => void;
}

const TILE_CLASS = 'w-40 h-40 rounded-xl object-cover bg-bambu-dark border border-bambu-dark-tertiary';
const ICON_BUTTON_CLASS =
  'p-1.5 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors disabled:opacity-50';

/**
 * The product's pictures, and the one of them that is its cover.
 *
 * ⚠️ **The cover has two shapes and they look identical here.** An EXPLICIT
 * cover is `cover_image_filename` — a picked picture or a dedicated upload —
 * and everything else falls back to the first picture by `sort_order`. Only the
 * explicit one can be cleared, so "clear cover" appears only when there is one;
 * offering it over the default would be a button that reports success and
 * changes nothing. `has_cover` is the EFFECTIVE answer and is what decides
 * whether the tile or the placeholder renders — never the column.
 *
 * ⚠️ **Order is data, not a render-time sort.** ↑ / ↓ post the whole ordered
 * list of the category, so the first picture (and therefore the default cover)
 * is something the operator sets rather than something the upload timestamps
 * decide.
 *
 * The lightbox is a plain fixed overlay: prev / next / Escape, no new
 * dependency for what is three keyboard handlers and an `<img>`.
 */
export function ProductGallery({
  product,
  canEdit,
  testIdSuffix = '',
  headingKey = 'products.gallery.title',
  onLightboxOpenChange,
}: ProductGalleryProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const pictureInput = useRef<HTMLInputElement>(null);
  const coverInput = useRef<HTMLInputElement>(null);
  const [lightbox, setLightbox] = useState<number | null>(null);
  const headingId = useId();

  const testId = (name: string) => `${name}${testIdSuffix}`;

  // ⚠️ The tie-break is the server's, not "whatever order the array arrived
  // in": `sorted_attachments` orders by `(sort_order, filename)`, and two
  // pictures at the same `sort_order` are ordinary — every upload into an empty
  // category starts at 0, and a reorder that names only some of them leaves the
  // rest sharing a rank. Without the second key the star could sit on a
  // different picture than the one `/cover-image` actually serves. The rule is
  // `byAttachmentOrder`, shared with `ProductAttachments` — two components
  // ordering the same column by different rules is the drift it removes.
  const pictures: ProductAttachment[] = (product.attachments ?? [])
    .filter((attachment) => attachment.category === 'pictures')
    .sort(byAttachmentOrder);

  // ⚠️ The EFFECTIVE cover, by the same rule the server's `effective_cover`
  // uses: the explicit column, else the first picture by `sort_order`. Null
  // here means no picture in this gallery is the cover — either the product has
  // none, or the explicit cover is a DEDICATED upload that deliberately never
  // joined the gallery.
  const coverFilename = product.cover_image_filename ?? pictures[0]?.filename ?? null;

  // ⚠️ Focus moves INTO the overlay when it opens and back to the thumbnail
  // when it closes — and that is ALL it does. **Tab is not trapped**: it walks
  // out of the overlay and into the page behind, exactly as it does anywhere
  // else in this app. What the move fixes is the two ends: a keyboard user who
  // opened the lightbox would otherwise start at the top of the document, and
  // on close the focus ring would be left on `<body>`, which is nowhere.
  // Trapping means inert-ing the rest of the page; it is deliberately not done
  // here, and this comment is not to grow a claim that it is.
  const overlay = useDialogFocus<HTMLDivElement>(lightbox !== null);

  // Announce the overlay to whatever is around us — see `onLightboxOpenChange`.
  // Reported from an effect rather than from each `setLightbox` call site so
  // that closing by Escape, by the ✕, by the backdrop and by an unmount all
  // report the same thing; missing one of those would leave a dialog that has
  // stood down from Escape permanently.
  //
  // ⚠️ Through a ref, so the effect depends on the OPEN FLAG alone. With the
  // callback in the dependency list an inline arrow from the parent would
  // re-run this on every render — and since the callback sets the parent's
  // state, that is a render loop rather than a wasted call.
  //
  // ⚠️ And the ref is refreshed in an EFFECT, never in the render body. Writing
  // to a ref while rendering is a side effect in a function React is allowed to
  // call twice and throw one result away (StrictMode does exactly that, and so
  // does a render it abandons for a higher-priority update) — the rule this
  // codebase follows everywhere else. Declared BEFORE the effect below, so on
  // the commit where `open` flips the callback is already the current one.
  const open = lightbox !== null;
  const notify = useRef(onLightboxOpenChange);
  useEffect(() => {
    notify.current = onLightboxOpenChange;
  });
  useEffect(() => {
    notify.current?.(open);
    return () => notify.current?.(false);
  }, [open]);

  useEffect(() => {
    if (lightbox === null || pictures.length === 0) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setLightbox(null);
      if (e.key === 'ArrowRight') setLightbox((i) => (i === null ? null : (i + 1) % pictures.length));
      if (e.key === 'ArrowLeft') setLightbox((i) => (i === null ? null : (i + pictures.length - 1) % pictures.length));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [lightbox, pictures.length]);

  // Every mutation touches the product row, the catalog cards and the order
  // cards — all three render this cover.
  //
  // ⚠️ **No cache-busting query parameter.** `GET /products/{id}/cover-image`
  // answers `Cache-Control: private, no-cache`, which makes the browser
  // revalidate that stable url on every render — so a version counter here
  // would only add a second, weaker answer to the same question, and one that
  // every other renderer of the cover (`ProductCard`, `OrderCard`) does not
  // have. One rule, and it lives on the response.
  const done = () => {
    // ⚠️ One call: the product keys are order views since Ruling 29, and they
    // are wanted here for their own sake as well — the first picture is the
    // implicit cover an order card renders.
    invalidateOrderViews(queryClient);
  };
  const fail = (e: Error) => showToast(e.message, 'error');

  const uploadPicture = useMutation({
    mutationFn: (file: File) => api.uploadProductAttachment(product.id, file, 'pictures'),
    onSuccess: done,
    onError: fail,
  });

  const uploadCover = useMutation({
    mutationFn: (file: File) => api.uploadProductCover(product.id, file),
    onSuccess: done,
    onError: fail,
  });

  const pickCover = useMutation({
    mutationFn: (filename: string) => api.setProductCover(product.id, filename),
    onSuccess: done,
    onError: fail,
  });

  const clearCover = useMutation({
    mutationFn: () => api.deleteProductCover(product.id),
    onSuccess: done,
    onError: fail,
  });

  const remove = useMutation({
    mutationFn: (filename: string) => api.deleteProductAttachment(product.id, filename),
    onSuccess: done,
    onError: fail,
  });

  const reorder = useMutation({
    mutationFn: (filenames: string[]) => api.reorderProductAttachments(product.id, 'pictures', filenames),
    onSuccess: done,
    onError: fail,
  });

  const move = (index: number, delta: number) => {
    const order = pictures.map((p) => p.filename);
    const target = index + delta;
    if (target < 0 || target >= order.length) return;
    [order[index], order[target]] = [order[target], order[index]];
    reorder.mutate(order);
  };

  const busy =
    uploadPicture.isPending ||
    uploadCover.isPending ||
    pickCover.isPending ||
    clearCover.isPending ||
    remove.isPending ||
    reorder.isPending;

  return (
    <section
      className="space-y-3"
      aria-labelledby={headingId}
      data-testid={testId('product-gallery')}
    >
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 id={headingId} className="text-lg font-semibold text-white flex items-center gap-2">
            <Image className="w-5 h-5" />
            {t(headingKey)}
          </h2>
          <p className="text-xs text-bambu-gray">{t('products.gallery.coverHint')}</p>
        </div>

        {canEdit && (
          <div className="flex items-center gap-2 flex-wrap">
            <input
              ref={pictureInput}
              data-testid={testId('gallery-upload-input')}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) uploadPicture.mutate(file);
                e.target.value = '';
              }}
            />
            <input
              ref={coverInput}
              data-testid={testId('gallery-cover-input')}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) uploadCover.mutate(file);
                e.target.value = '';
              }}
            />
            <Button type="button" variant="secondary" size="sm" onClick={() => pictureInput.current?.click()} disabled={busy}>
              {uploadPicture.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
              {t('products.gallery.uploadPicture')}
            </Button>
            <Button type="button" variant="secondary" size="sm" onClick={() => coverInput.current?.click()} disabled={busy}>
              {uploadCover.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Star className="w-4 h-4" />}
              {t('products.gallery.uploadCover')}
            </Button>
            {product.cover_image_filename && (
              <Button type="button" variant="secondary" size="sm" onClick={() => clearCover.mutate()} disabled={busy}>
                <X className="w-4 h-4" />
                {t('products.gallery.clearCover')}
              </Button>
            )}
          </div>
        )}
      </div>

      <div className="flex gap-4 flex-wrap items-start">
        {product.has_cover ? (
          <img
            data-testid={testId('product-gallery-cover')}
            src={api.getProductCoverImageUrl(product.id)}
            alt={t('products.gallery.cover')}
            className={TILE_CLASS}
          />
        ) : (
          <div
            data-testid={testId('product-cover-placeholder')}
            className={`${TILE_CLASS} flex items-center justify-center`}
          >
            <Package className="w-8 h-8 text-bambu-gray" />
          </div>
        )}

        {pictures.length > 0 ? (
          <ul className="flex gap-3 flex-wrap">
            {pictures.map((picture, index) => (
              <li
                key={picture.filename}
                className="w-28 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary p-1.5 space-y-1"
              >
                <button
                  type="button"
                  data-testid={testId(`gallery-picture-${picture.filename}`)}
                  onClick={() => setLightbox(index)}
                  className="block w-full"
                >
                  <img
                    src={api.getProductAttachmentImageUrl(product.id, picture.filename)}
                    alt={picture.original_name}
                    className="w-full h-24 rounded object-cover bg-bambu-dark"
                  />
                </button>
                {canEdit && (
                  <div className="flex items-center justify-between">
                    {/* ⚠️ The picture that IS the cover cannot be set as the
                        cover: the PUT would succeed and change nothing, which
                        is a button that reports success having done no work. It
                        is the EFFECTIVE cover that is marked, so the implicit
                        first picture is starred exactly like a picked one —
                        they are the same thing on screen and in the response. */}
                    <button
                      type="button"
                      className={ICON_BUTTON_CLASS}
                      disabled={busy || coverFilename === picture.filename}
                      aria-label={
                        coverFilename === picture.filename
                          ? `${t('products.gallery.isCover')}: ${picture.original_name}`
                          : `${t('products.gallery.setCover')}: ${picture.original_name}`
                      }
                      title={
                        coverFilename === picture.filename
                          ? t('products.gallery.isCover')
                          : t('products.gallery.setCover')
                      }
                      onClick={() => pickCover.mutate(picture.filename)}
                    >
                      <Star
                        className={`w-3.5 h-3.5 ${
                          coverFilename === picture.filename ? 'text-bambu-green fill-bambu-green' : ''
                        }`}
                      />
                    </button>
                    {index > 0 && (
                      <button
                        type="button"
                        className={ICON_BUTTON_CLASS}
                        disabled={busy}
                        aria-label={`${t('products.gallery.moveUp')}: ${picture.original_name}`}
                        onClick={() => move(index, -1)}
                      >
                        <ChevronUp className="w-3.5 h-3.5" />
                      </button>
                    )}
                    {index < pictures.length - 1 && (
                      <button
                        type="button"
                        className={ICON_BUTTON_CLASS}
                        disabled={busy}
                        aria-label={`${t('products.gallery.moveDown')}: ${picture.original_name}`}
                        onClick={() => move(index, 1)}
                      >
                        <ChevronDown className="w-3.5 h-3.5" />
                      </button>
                    )}
                    <button
                      type="button"
                      className={`${ICON_BUTTON_CLASS} text-status-error`}
                      disabled={busy}
                      aria-label={`${t('products.gallery.removePicture')}: ${picture.original_name}`}
                      onClick={() => remove.mutate(picture.filename)}
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-bambu-gray/70 italic self-center">{t('products.gallery.empty')}</p>
        )}
      </div>

      {lightbox !== null && pictures[lightbox] && (
        <div
          ref={overlay}
          role="dialog"
          aria-modal="true"
          aria-label={t('products.gallery.lightbox')}
          tabIndex={-1}
          data-testid={testId('gallery-lightbox')}
          className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 outline-none"
          onClick={() => setLightbox(null)}
        >
          <button
            type="button"
            aria-label={t('products.gallery.close')}
            className="absolute top-4 right-4 p-2 text-white/70 hover:text-white"
            onClick={() => setLightbox(null)}
          >
            <X className="w-6 h-6" />
          </button>
          {pictures.length > 1 && (
            <button
              type="button"
              aria-label={t('products.gallery.previous')}
              className="absolute left-4 p-2 text-white/70 hover:text-white"
              onClick={(e) => {
                e.stopPropagation();
                setLightbox((i) => (i === null ? null : (i + pictures.length - 1) % pictures.length));
              }}
            >
              <ChevronLeft className="w-8 h-8" />
            </button>
          )}
          <img
            data-testid={testId('gallery-lightbox-image')}
            src={api.getProductAttachmentImageUrl(product.id, pictures[lightbox].filename)}
            alt={pictures[lightbox].original_name}
            className="max-h-[85vh] max-w-full rounded-lg"
            onClick={(e) => e.stopPropagation()}
          />
          {pictures.length > 1 && (
            <button
              type="button"
              aria-label={t('products.gallery.next')}
              className="absolute right-4 p-2 text-white/70 hover:text-white"
              onClick={(e) => {
                e.stopPropagation();
                setLightbox((i) => (i === null ? null : (i + 1) % pictures.length));
              }}
            >
              <ChevronRight className="w-8 h-8" />
            </button>
          )}
        </div>
      )}
    </section>
  );
}
