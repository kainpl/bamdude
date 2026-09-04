import { useEffect, useId, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Loader2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type { Product, ProductCreate, ProductListItem, ProductUpdate } from '../../api/client';
import { Card, CardContent } from '../Card';
import { Button } from '../Button';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { ProductGallery } from './ProductGallery';
import { useDialogFocus } from '../../hooks/useDialogFocus';
import { useProductDetail } from '../../hooks/useProductDetail';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

const FIELD_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none';
const LABEL_CLASS = 'block text-sm font-medium text-white mb-1';

interface ProductCardDialogProps {
  product?: Product | ProductListItem | null;
  onClose: () => void;
}

/** A list row carries none of the descriptive fields — not "empty", absent. */
function isFullProduct(product: Product | ProductListItem | null | undefined): product is Product {
  return !!product && 'description' in product;
}

function Shell({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const { t } = useTranslation();
  const titleId = useId();
  // Mounted only while it is open, so "open" is simply `true`.
  const dialog = useDialogFocus<HTMLDivElement>(true);

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      {/* ⚠️ The role, the name and the focus, as one unit — see
          `useDialogFocus`, which lists every overlay that uses it and says
          exactly what it does and does not do. Without them the overlay is an
          anonymous `<div>` a screen reader never announces, and a keyboard user
          opening it starts at the top of the PAGE behind.
          They sit on a wrapper rather than on the `Card`, whose props are
          `HTMLAttributes` and so admit no `ref`; giving the shared component
          one would hand a `ref` to `CardHeader` and `CardContent` too, which
          spread nothing — a prop that type-checks and does nothing. */}
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="w-full max-w-2xl outline-none"
      >
        <Card className="w-full max-h-[90vh] overflow-y-auto">
          <CardContent className="p-0">
            <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
              <h2 id={titleId} className="text-xl font-semibold text-white">
                {title}
              </h2>
              <button
                type="button"
                onClick={onClose}
                aria-label={t('common.close')}
                className="text-bambu-gray hover:text-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
            {children}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

/**
 * The product card in one dialog: the fields somebody types, and the gallery.
 *
 * Opened from a card, it is handed a `ProductListItem`, which carries only the
 * counts — so the full product is fetched first and the form is not mounted
 * until it arrives. `OrderModal` solves the same trap by HIDING the fields a
 * list row lacks; here that would leave an edit dialog with nothing but a name
 * box, so the record is fetched instead. Either way the rule holds: no input
 * ever shows blank over text the user was not shown, because whatever is typed
 * into it REPLACES that text.
 *
 * ⚠️ **The gallery half exists only in edit mode**, and that is not a layout
 * preference: an upload needs a product id to hang the file on, and a product
 * being created does not have one yet. The create flow is the pass-2 one,
 * untouched.
 *
 * There is deliberately no `onSaved` callback — the same decision
 * `CustomerModal` and `OrderModal` record: both call sites just close the
 * dialog, and the saved record reaches every list through the invalidations
 * below. A prop nobody passes is a second way to learn the same fact, and the
 * one that goes uncalled when somebody adds a third call site.
 */
export function ProductCardDialog({ product, onClose }: ProductCardDialogProps) {
  const { t } = useTranslation();
  const needsFetch = !!product && !isFullProduct(product);
  // ⚠️ Through the shared hook, and `null` rather than an `enabled` flag of its
  // own. TanStack gives a query the LAST observer's options, so the dialog's
  // own `useQuery` took `meta: { refreshToast: true }` off the product page
  // underneath for as long as it was open — a failed background refetch then
  // said nothing on the page the flag exists for. See `useProductDetail`.
  const { data: fetched, error } = useProductDetail(needsFetch ? product!.id : null);
  const [lightboxOpen, setLightboxOpen] = useState(false);

  // ⚠️ **Escape belongs to the innermost overlay.** The gallery below opens a
  // lightbox with its own Escape handler, and both listeners sit on `window` —
  // ours is registered first (on mount; the gallery's only once a picture is
  // enlarged), so it ran first and closed the whole dialog, discarding
  // everything typed into the form, while the lightbox the user was actually
  // dismissing went with it. Standing down while the gallery reports a lightbox
  // open is the same ordering `ModelCardModal` writes as
  // `if (lightbox) … else onClose()`; there the state is its own, here it lives
  // one component down and comes back through `onLightboxOpenChange`.
  //
  // Reading `lightboxOpen` from the render closure is correct and not a race:
  // the gallery's `setLightbox(null)` is queued, not applied, while this handler
  // runs for the SAME key press — so the first Escape closes the lightbox and
  // the second, off the next render, closes the dialog.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !lightboxOpen) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, lightboxOpen]);

  const loaded = needsFetch ? fetched : (product as Product | null | undefined);
  if (needsFetch && !loaded) {
    // A failed fetch says so — a spinner that never stops is the same screen as
    // a slow server, and one of the two never ends.
    return (
      <Shell title={t('products.modal.editTitle')} onClose={onClose}>
        <div className="p-8 flex justify-center">
          {error ? (
            <p className="text-sm text-red-500">{(error as Error).message}</p>
          ) : (
            <Loader2 className="w-6 h-6 text-bambu-green animate-spin" />
          )}
        </div>
      </Shell>
    );
  }

  return <ProductForm product={loaded ?? null} onClose={onClose} onLightboxOpenChange={setLightboxOpen} />;
}

interface ProductFormProps {
  product: Product | null;
  onClose: () => void;
  /** Passed straight through to the gallery — see the Escape note above. */
  onLightboxOpenChange: (open: boolean) => void;
}

/** Split out so every field can be seeded by `useState` from a record that is
 *  already in hand — a form whose initial values arrive later would have to
 *  re-seed itself and could overwrite what the user has already typed. */
function ProductForm({ product, onClose, onLightboxOpenChange }: ProductFormProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const isEdit = !!product;

  const initial = {
    name: product?.name ?? '',
    description: product?.description ?? '',
    designer: product?.designer ?? '',
    license: product?.license ?? '',
    sourceUrl: product?.source_url ?? '',
    designId: product?.design_id ?? '',
    notes: product?.notes ?? '',
  };

  const [name, setName] = useState(initial.name);
  const [description, setDescription] = useState(initial.description);
  const [designer, setDesigner] = useState(initial.designer);
  const [license, setLicense] = useState(initial.license);
  const [sourceUrl, setSourceUrl] = useState(initial.sourceUrl);
  const [designId, setDesignId] = useState(initial.designId);
  const [notes, setNotes] = useState(initial.notes);

  const mutation = useMutation({
    mutationFn: () => {
      const trimmed = {
        name: name.trim(),
        description: description.trim(),
        designer: designer.trim(),
        license: license.trim(),
        source_url: sourceUrl.trim(),
        design_id: designId.trim(),
        notes: notes.trim(),
      };
      if (product) {
        const data: ProductUpdate = {};
        if (trimmed.name !== initial.name) data.name = trimmed.name;
        if (trimmed.description !== initial.description) data.description = trimmed.description || null;
        if (trimmed.designer !== initial.designer) data.designer = trimmed.designer || null;
        if (trimmed.license !== initial.license) data.license = trimmed.license || null;
        if (trimmed.source_url !== initial.sourceUrl) data.source_url = trimmed.source_url || null;
        if (trimmed.design_id !== initial.designId) data.design_id = trimmed.design_id || null;
        if (trimmed.notes !== initial.notes) data.notes = trimmed.notes || null;
        return api.updateProduct(product.id, data);
      }
      const data: ProductCreate = {
        name: trimmed.name,
        description: trimmed.description || null,
        designer: trimmed.designer || null,
        license: trimmed.license || null,
        source_url: trimmed.source_url || null,
        design_id: trimmed.design_id || null,
        notes: trimmed.notes || null,
      };
      return api.createProduct(data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['products'] });
      if (product) queryClient.invalidateQueries({ queryKey: ['product', product.id] });
      // ⚠️ The order views too, and this is the dialog that actually renames a
      // product: `ProjectLineResponse.product_name` is denormalised, so an
      // order card and every order line kept the OLD name for as long as their
      // `staleTime` said the answer was fresh. Same one decision as an order
      // save — see `utils/queryInvalidation`.
      invalidateOrderViews(queryClient);
      showToast(t('products.toast.saved'));
      onClose();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const canSubmit = name.trim() !== '' && !mutation.isPending;

  const textField = (
    id: string,
    label: string,
    value: string,
    setValue: (v: string) => void,
    type: 'text' | 'url' = 'text',
    required = false,
  ) => (
    <div>
      <label className={LABEL_CLASS} htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        type={type}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className={FIELD_CLASS}
        disabled={mutation.isPending}
        required={required}
      />
    </div>
  );

  return (
    <Shell title={isEdit ? t('products.modal.editTitle') : t('products.modal.createTitle')} onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) mutation.mutate();
        }}
      >
        <div className="p-4 space-y-4">
          {textField('product-name', t('products.modal.name'), name, setName, 'text', true)}

          <div>
            <label className={LABEL_CLASS} htmlFor="product-description">
              {t('products.modal.description')}
            </label>
            <textarea
              id="product-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className={`${FIELD_CLASS} min-h-[72px]`}
              disabled={mutation.isPending}
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            {textField('product-designer', t('products.modal.designer'), designer, setDesigner)}
            {textField('product-license', t('products.modal.license'), license, setLicense)}
          </div>

          <div className="grid grid-cols-2 gap-4">
            {textField('product-source-url', t('products.modal.sourceUrl'), sourceUrl, setSourceUrl, 'url')}
            {textField('product-design-id', t('products.modal.designId'), designId, setDesignId)}
          </div>

          <div>
            <label className={LABEL_CLASS} htmlFor="product-notes">
              {t('products.modal.notes')}
            </label>
            <textarea
              id="product-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className={`${FIELD_CLASS} min-h-[72px]`}
              disabled={mutation.isPending}
            />
          </div>

          {/* The gallery writes through its own routes, immediately — it is not
              part of this form's submit, and a picture uploaded here stays
              uploaded whether or not the fields are saved. */}
          {product && (
            <div className="pt-4 border-t border-bambu-dark-tertiary">
              {/* ⚠️ `testIdSuffix` — the product page renders its own gallery
                  and this dialog opens OVER it, so without the suffix every
                  `getByTestId` in a page test would find two. `headingKey` is
                  the same problem for the people the page is for: two live
                  regions both called "Pictures" is what a screen reader hears
                  while this dialog is open. */}
              <ProductGallery
                product={product}
                canEdit={hasPermission('projects:update')}
                testIdSuffix="-dialog"
                headingKey="products.gallery.titleInDialog"
                onLightboxOpenChange={onLightboxOpenChange}
              />
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 p-4 border-t border-bambu-dark-tertiary">
          <Button type="button" variant="secondary" onClick={onClose} disabled={mutation.isPending}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={!canSubmit}>
            {mutation.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : isEdit ? (
              t('products.modal.save')
            ) : (
              t('products.modal.create')
            )}
          </Button>
        </div>
      </form>
    </Shell>
  );
}
