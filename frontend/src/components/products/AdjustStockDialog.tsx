import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';
import { Modal } from '../Modal';
import { Select } from '../Select';

const FIELD_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none';

interface AdjustStockDialogProps {
  productId: number;
  parts: { part_id: number; name: string }[];
  onClose: () => void;
  /** The Stock tab invalidates its own two keys here; the product page needs
   *  nothing beyond the defaults below. */
  onSaved?: () => void;
}

/**
 * The hand correction: the operator counted the shelf and it disagreed with us.
 *
 * Only COUNTED parts are offered, because they are the only ones that hold a
 * balance — the server answers 422 for any other, and offering a part whose
 * only possible outcome is an error is worse than not offering it.
 *
 * ⚠️ It rides the shared shell like every other dialog here, which is what
 * carries the `role`, the `aria-modal`, the name, the focus and Escape
 * (finding M1). An overlay without them is an anonymous `<div>` a screen reader
 * never announces, and a keyboard user who opens it starts at the top of the
 * page behind.
 */
export function AdjustStockDialog({ productId, parts, onClose, onSaved }: AdjustStockDialogProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [partId, setPartId] = useState<number>(parts[0]?.part_id ?? 0);
  const [delta, setDelta] = useState('1');
  const [note, setNote] = useState('');

  const adjust = useMutation({
    mutationFn: () => api.adjustProductStock(productId, { part_id: partId, delta: Number(delta), note: note.trim() }),
    onSuccess: () => {
      // The shelf, the product's own `kits_available`, and the catalog card
      // that shows it. No order view moves: a hand correction changes what is
      // free, never what a line has already reserved.
      queryClient.invalidateQueries({ queryKey: ['product-stock', productId] });
      queryClient.invalidateQueries({ queryKey: ['product', productId] });
      queryClient.invalidateQueries({ queryKey: ['products'] });
      onSaved?.();
      showToast(t('stock.adjust.saved'));
      onClose();
    },
    // 409 (would go below zero) and 422 (not a counted part) both arrive as the
    // server's own sentence in `detail`, which is what `ApiError.message` is.
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const parsed = Number(delta);
  const valid = Number.isInteger(parsed) && parsed !== 0 && note.trim().length > 0 && partId > 0;

  return (
    <Modal onClose={onClose} title={t('stock.adjust.title')} size="md">
      <div className="p-4 space-y-3">
        <div>
          <label htmlFor="stock-adjust-part" className="block text-sm text-bambu-gray mb-1">
            {t('stock.adjust.part')}
          </label>
          <Select
            className="w-full"
            id="stock-adjust-part"
            value={partId}
            onChange={(e) => setPartId(Number(e.target.value))}
          >
            {parts.map((p) => (
              <option key={p.part_id} value={p.part_id}>
                {p.name}
              </option>
            ))}
          </Select>
        </div>

        <div>
          <label htmlFor="stock-adjust-delta" className="block text-sm text-bambu-gray mb-1">
            {t('stock.adjust.delta')}
          </label>
          <input
            id="stock-adjust-delta"
            type="number"
            value={delta}
            onChange={(e) => setDelta(e.target.value)}
            className={FIELD_CLASS}
          />
        </div>

        <div>
          <label htmlFor="stock-adjust-note" className="block text-sm text-bambu-gray mb-1">
            {t('stock.adjust.note')}
          </label>
          <input
            id="stock-adjust-note"
            type="text"
            maxLength={500}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder={t('stock.adjust.notePlaceholder')}
            className={FIELD_CLASS}
          />
        </div>
      </div>

      <div className="p-4 border-t border-bambu-dark-tertiary flex gap-3">
        <Button type="button" variant="secondary" onClick={onClose} className="flex-1">
          {t('common.cancel')}
        </Button>
        <Button
          type="button"
          onClick={() => adjust.mutate()}
          disabled={!valid || adjust.isPending}
          className="flex-1"
          data-testid="stock-adjust-submit"
        >
          {t('stock.adjust.submit')}
        </Button>
      </div>
    </Modal>
  );
}
