import { useTranslation } from 'react-i18next';
import { PackageX } from 'lucide-react';
import type { ArchivePart } from '../api/client';

const FIELD = 'px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none';

interface DefectsFieldsProps {
  /** The print's part rows; empty → one flat counter against `quantity`. */
  parts: ArchivePart[];
  /** Per-part values keyed by row id (absolute). */
  values: Record<number, number>;
  onChange: (partId: number, next: number) => void;
  /** The print's quantity — the flat counter's ceiling. */
  quantity: number;
  flat: number;
  onFlatChange: (next: number) => void;
}

/**
 * The defect counters, shared by the archive editor, the order page's dialog
 * and the printer card's plate-clear block — one form, so scrap is entered
 * the same way everywhere. Per part when the print has rows (each clamped to
 * that part's own quantity), else one flat total clamped to the print's.
 */
export function DefectsFields({ parts, values, onChange, quantity, flat, onFlatChange }: DefectsFieldsProps) {
  const { t } = useTranslation();
  const sum = Object.values(values).reduce((acc, n) => acc + n, 0);

  if (parts.length > 0) {
    return (
      <div>
        <label className="block text-sm text-bambu-gray mb-1">
          <PackageX className="w-4 h-4 inline mr-1" />
          {t('editArchive.partsDefectiveTitle')}
        </label>
        <div className="space-y-2">
          {parts.map((part) => (
            <div key={part.id} className="flex items-center justify-between gap-3">
              <span className="text-sm text-white truncate flex-1">{part.name}</span>
              <span className="text-xs text-bambu-gray whitespace-nowrap">&times; {part.quantity}</span>
              <input
                type="number"
                min={0}
                max={part.quantity}
                value={values[part.id] ?? 0}
                onChange={(e) => {
                  const raw = parseInt(e.target.value) || 0;
                  onChange(part.id, Math.min(part.quantity, Math.max(0, raw)));
                }}
                data-testid={`part-defective-${part.id}`}
                className={`w-20 ${FIELD}`}
              />
            </div>
          ))}
        </div>
        <p className="text-sm text-white mt-2" data-testid="parts-defective-total">
          {t('editArchive.partsDefectiveTotal')}: {sum}
        </p>
        <p className="text-xs text-bambu-gray mt-1">{t('editArchive.partsDefectiveHelp')}</p>
      </div>
    );
  }

  return (
    <div>
      <label className="block text-sm text-bambu-gray mb-1">
        <PackageX className="w-4 h-4 inline mr-1" />
        {t('editArchive.defectiveParts')}
      </label>
      <input
        type="number"
        min={0}
        max={quantity}
        value={flat}
        onChange={(e) => onFlatChange(Math.min(quantity, Math.max(0, parseInt(e.target.value) || 0)))}
        data-testid="defective-count-input"
        className={`w-full ${FIELD}`}
        placeholder="0"
      />
      <p className="text-xs text-bambu-gray mt-1">{t('editArchive.defectivePartsHelp')}</p>
    </div>
  );
}
