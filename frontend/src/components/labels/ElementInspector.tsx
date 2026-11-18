/**
 * What the selected box says, and exactly where it sits.
 *
 * The canvas is for arranging; this is for the things a mouse cannot express —
 * which field a line shows, what happens when it does not fit, and the
 * millimetre somebody wants rather than the one they could hit.
 */
import { useTranslation } from 'react-i18next';
import { NumberInput } from '../NumberInput';
import { useQuery } from '@tanstack/react-query';
import { Trash2 } from 'lucide-react';
import { api, type LabelPlaceholder, type LabelSwatchShape, type LabelTemplateElement } from '../../api/client';
import { Button } from '../Button';
import { MIN_SIDE_MM, roundMm } from './labelGeometry';
import { Select } from '../Select';

interface ElementInspectorProps {
  element: LabelTemplateElement;
  widthMm: number;
  heightMm: number;
  onChange: (next: LabelTemplateElement) => void;
  onDelete: () => void;
}

const SYMBOLOGIES = ['ean13', 'code128', 'code39', 'ean8', 'upca', 'itf'] as const;

export function ElementInspector({
  element,
  widthMm,
  heightMm,
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
        <Button variant="secondary" size="sm" onClick={onDelete}>
          <Trash2 className="w-4 h-4 text-red-600 dark:text-red-400" />
        </Button>
      </div>

      <label className="block">
        <span className="text-xs text-bambu-gray">{t('labelEditor.content')}</span>
        <input
          value={element.content}
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
                onClick={() => insertPlaceholder(placeholder.key)}
                title={`${t(`labelEditor.placeholders.${placeholder.key}.description`, {
                  defaultValue: placeholder.description,
                })} — ${t('labelEditor.example')}: ${placeholder.example}`}
                className="px-1.5 py-0.5 text-xs bg-bambu-dark border border-bambu-dark-tertiary rounded hover:border-bambu-green disabled:opacity-50"
              >
                {/* The vocabulary is the server's; its WORDING is the reader's.
                    A key the locale does not know keeps the server's label. */}
                {t(`labelEditor.placeholders.${placeholder.key}.label`, { defaultValue: placeholder.label })}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="grid grid-cols-4 gap-2">
        {(['x_mm', 'y_mm', 'w_mm', 'h_mm'] as const).map((field) => (
          <label key={field} className="block">
            <span className="text-xs text-bambu-gray">{t(`labelEditor.${field}`)}</span>
            <NumberInput
              step="0.5"
              min={field === 'w_mm' || field === 'h_mm' ? MIN_SIDE_MM : 0}
              max={field === 'x_mm' || field === 'w_mm' ? widthMm : heightMm}
              value={element[field]}
              onChange={(v) => patch({ [field]: roundMm(v) })}
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
              <NumberInput
                step="0.5"
                min={0.5}
                value={element.size_mm}
                onChange={(v) => patch({ size_mm: roundMm(v) })}
                className="w-full mt-1 px-1.5 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
              />
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('labelEditor.fit')}</span>
              <Select
                size="sm"
                className="w-full mt-1"
                value={element.fit}
                onChange={(e) => patch({ fit: e.target.value as 'shrink' | 'clip' })}
              >
                {/* ⚠️ Shrinking inverts the type hierarchy on real data — a long
                    brand ends up smaller than the short material line under it.
                    Both are offered because both are sometimes right. */}
                <option value="shrink">{t('labelEditor.fitShrink')}</option>
                <option value="clip">{t('labelEditor.fitClip')}</option>
              </Select>
            </label>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('labelEditor.align')}</span>
              <Select
                size="sm"
                className="w-full mt-1"
                value={element.align}
                onChange={(e) => patch({ align: e.target.value as 'left' | 'center' | 'right' })}
              >
                <option value="left">{t('labelEditor.alignLeft')}</option>
                <option value="center">{t('labelEditor.alignCenter')}</option>
                <option value="right">{t('labelEditor.alignRight')}</option>
              </Select>
            </label>
            <label className="block">
              <span className="text-xs text-bambu-gray">{t('labelEditor.valign')}</span>
              <Select
                size="sm"
                className="w-full mt-1"
                value={element.valign}
                onChange={(e) => patch({ valign: e.target.value as 'top' | 'middle' | 'bottom' })}
              >
                <option value="top">{t('labelEditor.valignTop')}</option>
                <option value="middle">{t('labelEditor.valignMiddle')}</option>
                <option value="bottom">{t('labelEditor.valignBottom')}</option>
              </Select>
            </label>
          </div>

          <div className="flex gap-3">
            <label className="flex items-center gap-1.5 text-xs text-bambu-gray">
              <input
                type="checkbox"
                checked={element.bold}
                onChange={(e) => patch({ bold: e.target.checked })}
              />
              {t('labelEditor.bold')}
            </label>
            <label className="accent-bambu-green flex items-center gap-1.5 text-xs text-bambu-gray">
              <input
                type="checkbox"
                checked={element.italic}
                onChange={(e) => patch({ italic: e.target.checked })}
              />
              {t('labelEditor.italic')}
            </label>
          </div>
        </>
      )}

      {element.type === 'barcode' && (
        <label className="accent-bambu-green block">
          <span className="text-xs text-bambu-gray">{t('labelEditor.symbology')}</span>
          <Select
            size="sm"
            className="w-full mt-1"
            value={element.symbology}
            onChange={(e) => patch({ symbology: e.target.value as (typeof SYMBOLOGIES)[number] })}
          >
            {SYMBOLOGIES.map((symbology) => (
              <option key={symbology} value={symbology}>
                {symbology}
              </option>
            ))}
          </Select>
          {/* EAN-13 wants exactly the digits it wants; the server refuses the
              rest with a warning rather than printing an unscannable code. */}
          <span className="block mt-1 text-xs text-bambu-gray">{t('labelEditor.symbologyHint')}</span>
        </label>
      )}

      {element.type === 'swatch' && (
        <>
          <label className="block text-xs text-bambu-gray">
            {t('labelEditor.swatchShape')}
            <Select
              size="sm"
              className="mt-1 w-full"
              value={element.shape ?? 'rect'}
              onChange={(e) => onChange({ ...element, shape: e.target.value as LabelSwatchShape })}
            >
              <option value="rect">{t('labelEditor.swatchShapeRect')}</option>
              <option value="circle">{t('labelEditor.swatchShapeCircle')}</option>
              <option value="rounded">{t('labelEditor.swatchShapeRounded')}</option>
            </Select>
          </label>
          <p className="text-xs text-bambu-gray">{t('labelEditor.swatchHint')}</p>
        </>
      )}
    </div>
  );
}
