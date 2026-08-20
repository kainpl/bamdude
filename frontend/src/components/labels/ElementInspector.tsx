/**
 * What the selected box says, and exactly where it sits.
 *
 * The canvas is for arranging; this is for the things a mouse cannot express —
 * which field a line shows, what happens when it does not fit, and the
 * millimetre somebody wants rather than the one they could hit.
 */
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { Trash2 } from 'lucide-react';
import { api, type LabelPlaceholder, type LabelTemplateElement } from '../../api/client';
import { Button } from '../Button';
import { MIN_SIDE_MM, roundMm } from './labelGeometry';

interface ElementInspectorProps {
  element: LabelTemplateElement;
  widthMm: number;
  heightMm: number;
  disabled: boolean;
  onChange: (next: LabelTemplateElement) => void;
  onDelete: () => void;
}

const SYMBOLOGIES = ['ean13', 'code128', 'code39', 'ean8', 'upca', 'itf'] as const;

export function ElementInspector({
  element,
  widthMm,
  heightMm,
  disabled,
  onChange,
  onDelete,
}: ElementInspectorProps) {
  const { t } = useTranslation();

  // ⚠️ Served, not duplicated. The picker and the renderer have to agree about
  // what `{remaining_g}` means, and a second hand-kept list is how they stop.
  const { data: placeholders } = useQuery({
    queryKey: ['label-placeholders'],
    queryFn: api.getLabelPlaceholders,
    staleTime: Infinity,
  });

  const patch = (fields: Partial<LabelTemplateElement>) =>
    onChange({ ...element, ...fields } as LabelTemplateElement);

  const number = (value: string, fallback: number) => {
    const parsed = Number(value.replace(',', '.'));
    return Number.isFinite(parsed) ? parsed : fallback;
  };

  const insertPlaceholder = (key: string) => {
    // Appended rather than replacing: a line is a sentence of fields, and
    // "one field" is only the simplest case of it.
    const separator = element.content && !element.content.endsWith(' ') ? ' ' : '';
    patch({ content: `${element.content}${separator}{${key}}` });
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-white">
          {t(`labelEditor.elementType.${element.type}`)}
        </span>
        <Button variant="secondary" size="sm" disabled={disabled} onClick={onDelete}>
          <Trash2 className="w-4 h-4 text-red-600 dark:text-red-400" />
        </Button>
      </div>

      <label className="block">
        <span className="text-xs text-bambu-gray">{t('labelEditor.content')}</span>
        <input
          value={element.content}
          disabled={disabled}
          onChange={(e) => patch({ content: e.target.value })}
          className="w-full mt-1 px-2 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
        />
      </label>

      {placeholders && (
        <div>
          <span className="text-xs text-bambu-gray">{t('labelEditor.fields')}</span>
          <div className="flex flex-wrap gap-1 mt-1 max-h-32 overflow-y-auto">
            {placeholders.map((placeholder: LabelPlaceholder) => (
              <button
                key={placeholder.key}
                type="button"
                disabled={disabled}
                onClick={() => insertPlaceholder(placeholder.key)}
                title={`${placeholder.description} — ${t('labelEditor.example')}: ${placeholder.example}`}
                className="px-1.5 py-0.5 text-xs bg-bambu-dark border border-bambu-dark-tertiary rounded hover:border-bambu-green disabled:opacity-50"
              >
                {placeholder.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="grid grid-cols-4 gap-2">
        {(['x_mm', 'y_mm', 'w_mm', 'h_mm'] as const).map((field) => (
          <label key={field} className="block">
            <span className="text-xs text-bambu-gray">{t(`labelEditor.${field}`)}</span>
            <input
              type="number"
              step="0.5"
              value={element[field]}
              disabled={disabled}
              onChange={(e) => {
                const raw = number(e.target.value, element[field]);
                const limit =
                  field === 'x_mm' || field === 'w_mm' ? widthMm : heightMm;
                const floor = field === 'w_mm' || field === 'h_mm' ? MIN_SIDE_MM : 0;
                patch({ [field]: roundMm(Math.min(Math.max(raw, floor), limit)) });
              }}
              className="w-full mt-1 px-1.5 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
            />
          </label>
        ))}
      </div>

      {element.type === 'text' && (
        <>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('labelEditor.sizeMm')}</span>
              <input
                type="number"
                step="0.5"
                value={element.size_mm}
                disabled={disabled}
                onChange={(e) => patch({ size_mm: roundMm(Math.max(0.5, number(e.target.value, element.size_mm))) })}
                className="w-full mt-1 px-1.5 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('labelEditor.fit')}</span>
              <select
                value={element.fit}
                disabled={disabled}
                onChange={(e) => patch({ fit: e.target.value as 'shrink' | 'clip' })}
                className="w-full mt-1 px-1.5 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
              >
                {/* ⚠️ Shrinking inverts the type hierarchy on real data — a long
                    brand ends up smaller than the short material line under it.
                    Both are offered because both are sometimes right. */}
                <option value="shrink">{t('labelEditor.fitShrink')}</option>
                <option value="clip">{t('labelEditor.fitClip')}</option>
              </select>
            </label>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('labelEditor.align')}</span>
              <select
                value={element.align}
                disabled={disabled}
                onChange={(e) => patch({ align: e.target.value as 'left' | 'center' | 'right' })}
                className="w-full mt-1 px-1.5 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
              >
                <option value="left">{t('labelEditor.alignLeft')}</option>
                <option value="center">{t('labelEditor.alignCenter')}</option>
                <option value="right">{t('labelEditor.alignRight')}</option>
              </select>
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('labelEditor.valign')}</span>
              <select
                value={element.valign}
                disabled={disabled}
                onChange={(e) => patch({ valign: e.target.value as 'top' | 'middle' | 'bottom' })}
                className="w-full mt-1 px-1.5 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
              >
                <option value="top">{t('labelEditor.valignTop')}</option>
                <option value="middle">{t('labelEditor.valignMiddle')}</option>
                <option value="bottom">{t('labelEditor.valignBottom')}</option>
              </select>
            </label>
          </div>

          <div className="flex gap-3">
            <label className="flex items-center gap-1.5 text-xs text-bambu-gray">
              <input
                type="checkbox"
                checked={element.bold}
                disabled={disabled}
                onChange={(e) => patch({ bold: e.target.checked })}
              />
              {t('labelEditor.bold')}
            </label>
            <label className="flex items-center gap-1.5 text-xs text-bambu-gray">
              <input
                type="checkbox"
                checked={element.italic}
                disabled={disabled}
                onChange={(e) => patch({ italic: e.target.checked })}
              />
              {t('labelEditor.italic')}
            </label>
          </div>
        </>
      )}

      {element.type === 'barcode' && (
        <label className="block">
          <span className="text-xs text-bambu-gray">{t('labelEditor.symbology')}</span>
          <select
            value={element.symbology}
            disabled={disabled}
            onChange={(e) => patch({ symbology: e.target.value as (typeof SYMBOLOGIES)[number] })}
            className="w-full mt-1 px-1.5 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
          >
            {SYMBOLOGIES.map((symbology) => (
              <option key={symbology} value={symbology}>
                {symbology}
              </option>
            ))}
          </select>
          {/* EAN-13 wants exactly the digits it wants; the server refuses the
              rest with a warning rather than printing an unscannable code. */}
          <span className="block mt-1 text-xs text-bambu-gray">{t('labelEditor.symbologyHint')}</span>
        </label>
      )}

      {element.type === 'swatch' && (
        <p className="text-xs text-bambu-gray">{t('labelEditor.swatchHint')}</p>
      )}
    </div>
  );
}
